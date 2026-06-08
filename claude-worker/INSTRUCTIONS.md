# Claude queue worker — 常駐セッションの運用手順

orchestrator は **`claude -p` を使わない**(別料金)。代わりに **ファイル queue** を境界にして、
ここに書かれた手順に従う **常駐 Claude Code セッション**がタスクを処理する(program_creater 方式)。

## 起動(人間が一度だけ)

**Windows(PowerShell)** — 新しいウィンドウ/タブで:
```powershell
cd C:\Users\hide\Documents\work\orchestration
.\tools\start-claude-worker.ps1
```
これが対話セッション(**`claude -p` ではない**)を `--permission-mode acceptEdits` で起動し、queue 監視を指示する。
`queue を見張っています` と出れば準備完了。

**手動で起動する場合** — 新ターミナルで `claude` を起動し、次を貼る:
> このリポジトリの `claude-worker/INSTRUCTIONS.md` に従い、queue worker として常駐して。

## worker のループ(Claude セッションがやること)

1. **heartbeat**: ループ毎に `queue/claude.alive` に現在時刻を **Write**(acceptEdits なら無確認で書ける)。
   orchestrator はこのファイルの新しさで「worker 稼働中」を判定する(既定 120 秒。古い/無ければ自動で codex に degrade)。
2. **inbox を監視**: `queue/inbox/*.json` のうち、対応する `queue/reports/<label>.json` がまだ無いものを探す。
3. 各タスク `{label, kind, schema, prompt}` について:
   - `prompt` に従い、**`schema` に厳密準拠した JSON だけ**を生成(説明文・コードフェンス禁止)。
   - 結果を `queue/reports/<label>.json` に JSON のみで書く。
4. 1〜3 を繰り返す(新規が来るまで待機)。

## ルール
- 出力は JSON のみ。`schema` の必須フィールドを全て埋める(N/A は空文字 / 空配列)。
- ARCHITECTURE.md の原則に従う(**候補を出すだけ**。良し悪しの自己採点はしない)。
- `queue/` の外を書き換えない。secret を読み書きしない。

## 停止
- セッションを終了すれば heartbeat が止まり、orchestrator は次の run から自動で codex のみに degrade する。

## 動作確認(常駐 Claude なしで配管だけ試す)
```bash
# 偽 worker を別ターミナルで起動(トークン不要)
python tools/mock_claude_worker.py 60
# 別ターミナルで dual を offline 実行(codex 側も mock に)
python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
```
