# queue worker — 常駐セッションの運用手順(codex / claude 共通)

orchestrator は **headless 呼び出し(codex exec / claude -p)を使わない**。代わりに **ファイル queue** を境界にして、
ここに書かれた手順に従う **常駐セッション**(codex も claude も同じ)がタスクを処理する(program_creater / Shogun 方式)。

engine ごとに独立した queue を持つ:
- `queue/<engine>/inbox/<label>.json` … orchestrator が書く依頼
- `queue/<engine>/reports/<label>.json` … worker が書く回答(JSON のみ)
- `queue/<engine>.alive` … heartbeat(生存信号)。**worker のループ(`wait-for-task.ps1`)が更新** = ループが生きている証

## 起動(人間が engine ごとに)

**Windows(PowerShell)** — 新しいウィンドウ/タブで:
```powershell
cd C:\path\to\claude-codex-orchestrator
.\tools\start-worker.ps1 codex      # codex worker
# 別ウィンドウで
.\tools\start-worker.ps1 claude     # claude worker
```
これが対話セッション(**headless ではない**)を起動し、queue 監視を指示する。
`<engine> queue を見張っています` と出れば準備完了。

**手動で起動する場合** — 新ターミナルで `codex`(または `claude`)を対話起動し、次を貼る:
> このリポジトリの `worker/INSTRUCTIONS.md` に従い、`<engine>` の queue worker として常駐して。

## worker のループ(セッションがやること)

**heartbeat は `wait-for-task.ps1` が更新する**(ポーリングのたびに `queue/<engine>.alive` を書く)。
つまり heartbeat の新しさ = この **worker ループが生きている証**。**ループを止めると heartbeat も止まり、orchestrator が正しく engine を外す**(別プロセスのタイマーではないので「生きてるフリ」をしない)。止まらず以下を繰り返す:

1. `pwsh -NoProfile -File tools/wait-for-task.ps1 <engine>` を実行(次タスクが来るまでブロック。待機中もターンを無駄にしない)。
2. 出力が `NO_TASK` なら 1 に戻る。ファイルパスが出たら、その JSON `{label, kind, schema, prompt}` を読む。
3. `prompt` に従い **`schema` に厳密準拠した JSON だけ**を生成(説明文・コードフェンス禁止)。まず
   `queue/<engine>/reports/<同名>.json.tmp` に書き、完成後 `queue/<engine>/reports/<同名>.json` へ rename / move(途中読み防止)。
4. 1 に戻る。**新しいタスクが来る限り捌き続ける**(generate の後に red-team / revise / verify のタスクが順次届く)。

> 万一セッションが待機して止まったら、worker タブに「continue(queue を捌き続けて)」と一声で再開。

## ルール
- 出力は JSON のみ。`schema` の必須フィールドを全て埋める(N/A は空文字 / 空配列)。
- ARCHITECTURE.md の原則に従う(**候補を出すだけ**。良し悪しの自己採点はしない)。
- `queue/<engine>/` の外を書き換えない。secret を読み書きしない。

## 停止
- セッションを終了すれば heartbeat が止まり、orchestrator は次の run からその engine を自動的に外す。
- 両 engine とも止まっていて mock も使わない場合、orchestrator は「worker を立てて」と表示して終了する。

## 動作確認(常駐セッションなしで配管だけ試す)
```powershell
python tools/mock_worker.py claude 120        # 別ターミナルで偽 worker(heartbeat + mock 応答)
python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
```
