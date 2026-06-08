# queue worker — 常駐セッションの運用手順(codex / claude 共通)

orchestrator は **headless 呼び出し(codex exec / claude -p)を使わない**。代わりに **ファイル queue** を境界にして、
ここに書かれた手順に従う **常駐セッション**(codex も claude も同じ)がタスクを処理する(program_creater / Shogun 方式)。

engine ごとに独立した queue を持つ:
- `queue/<engine>/inbox/<label>.json` … orchestrator が書く依頼
- `queue/<engine>/reports/<label>.json` … worker が書く回答(JSON のみ)
- `queue/<engine>.alive` … worker の heartbeat(生存信号)

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

1. **heartbeat**: ループ毎に `queue/<engine>.alive` に現在時刻を **Write**(書込許可があれば無確認で書ける)。
   orchestrator はこの新しさで「worker 稼働中」を判定する(既定 120 秒。古い/無ければ自動でその engine を除外)。
2. **inbox を監視**: `queue/<engine>/inbox/*.json` のうち、対応する `queue/<engine>/reports/<同名>.json` が無いものを探す。
3. 各タスク `{label, kind, schema, prompt}` について:
   - `prompt` に従い、**`schema` に厳密準拠した JSON だけ**を生成(説明文・コードフェンス禁止)。
   - 結果はまず `queue/<engine>/reports/<同名>.json.tmp` に JSON のみで書き、完成後に
     `queue/<engine>/reports/<同名>.json` へ rename / move する(途中読みを避ける)。
4. 1〜3 を繰り返す(新規が来るまで待機)。

## ルール
- 出力は JSON のみ。`schema` の必須フィールドを全て埋める(N/A は空文字 / 空配列)。
- ARCHITECTURE.md の原則に従う(**候補を出すだけ**。良し悪しの自己採点はしない)。
- `queue/<engine>/` の外を書き換えない。secret を読み書きしない。

## 停止
- セッションを終了すれば heartbeat が止まり、orchestrator は次の run からその engine を自動的に外す。
- 両 engine とも止まっていて mock も使わない場合、orchestrator は「worker を立てて」と表示して終了する。

## 動作確認(常駐セッションなしで配管だけ試す)
```bash
python tools/mock_worker.py claude 120        # 別ターミナルで偽 worker(heartbeat + mock 応答)
python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
```
