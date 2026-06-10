# ADR-001: エンジン実行モデル(ワーカー駆動方式)

- 状態: **Accepted**(2026-06-09)
- 関連: ARCHITECTURE.md §3.3 / §8、Issue #3(role別worker)、#27(dual default)、HANDOFF.md

## Context(背景)

候補生成エンジン(Codex / Claude)を「ワーカー」としてどう実行するか。ハード制約:

1. **Windows ネイティブ**(WSL を前提にしたくない)
2. **engine 中立**(Codex/Claude 対等。claude 中心の階層を持たない)
3. **`claude -p`(headless)は使わない**(別課金)。`codex exec` はサブスク内で問題ないが、純度のため両者を揃えたい
4. 信頼性(実機で確実に回る)

最初は「両エンジンとも常駐対話セッションが自分で queue をポーリングして処理(self-loop)」にしたが、実機で
**対話 LLM セッションは初回バーストを処理した後に idle 化**し、後続ステージのタスクを拾わない症状が出た。
LLM に daemon ループを任せるのは信頼できない、というのが判明した核心。

## Options(検討した選択肢)

| 案 | 概要 | 結論 |
|---|---|---|
| A. self-loop | LLM 自身が queue をポーリングし続ける | ✗ idle 化で不安定(実機で確認) |
| B. ハイブリッド | codex=headless(`codex exec`)/ claude=常駐 | △ 動くが「完全統一」でない。フォールバックとして保持 |
| **C. 外部駆動 interactive runner** | **supervisor が job 単位で対話セッションへ注入し、report で完了検知。ConPTY(pywinpty)で起動・駆動** | ✓ **採用** |
| D. WSL + tmux(shogun) | tmux の send-keys で wake | ✗ Windows ネイティブでない |
| E. shogun を Windows 移植 | — | ✗ 核が tmux→ConPTY 再実装 + bash 全書換 + claude中心ロール再設計 + 目的不一致。実質 C + 大量書換 |

重要な reframe(Codex レビューより):**LLM に常駐ループを任せず、supervisor がループを所有する**。
job を1個渡す → 対話セッションが処理して report file を書く → supervisor が検知 → 次を渡す。

## Decision(決定)

**外部駆動 interactive session runner を採用する。**

- 各 engine を **pywinpty(ConPTY)** で**対話モード**起動(= `-p`/`exec` でない = サブスク内・**追加課金なし**)。Windows ネイティブ、WSL 不要。
- **supervisor(orchestrator)が job 単位で wake/inject/observe**。LLM に daemon ループは持たせない。
- **完了は report ファイル(atomic rename)で判定**(TUI 出力ストリームのパースに依存しない)。
- engine 中立は「**同じ Runner 契約・同じ queue protocol・同じ artifact format**」で担保(実行機構の同一ではない)。
- **標準ライブラリ縛りは外し、`pywinpty` を正式依存にする**(maintained: 3.0.3 / 2026, ConPTY ラッパ)。
- D(WSL+tmux)と E(shogun 移植)は不採用。A は不採用。B は C が脆い場合のフォールバックとして残す。

## Evidence(根拠:スパイク `tools/conpty_spike.py`)

pywinpty で実機検証し、**両エンジンとも完全 PASS**:

```
RESULT[codex] : step1(seed job)=PASS   step2(inject 2nd job)=PASS
RESULT[claude]: step1(seed job)=PASS   step2(inject 2nd job)=PASS
```
- step1 = 対話起動したセッションに seed した job を自律処理させ `report` ファイルを生成。
- step2 = **同一セッションへ 2 個目の job を注入**して処理させる(supervisor 駆動の核)。

### 実装の肝(ハマりどころ)
- **注入は「テキスト」と「Enter」を別の write に分ける。** `write(text + "\r")` を1回でやると **paste 扱い**になり Enter が
  改行として入力欄に残るだけ(submit されない)。`write(text)` → 少し待つ → `write("\r")` で確定。両エンジンで有効。
- ctypes だけで ConPTY を自作する道(stdlib 縛り)も試したが、子プロセスの std ハンドル attach が finicky で時間対効果が悪い。
  **pywinpty が ConPTY を正しくラップしている**ので、これを使う。

## Consequences(影響)

- **手動 worker(start-worker.ps1 を別タブ)も self-loop も heartbeat も不要**になる見込み。
  orchestrator がセッションを spawn・駆動するので **`python orchestrate.py` だけで完全自動**。
- 新規依存 **pywinpty(Windows 専用)**。`requirements.txt` に追加。
- **TUI 駆動は version 依存で脆い面がある**(submit キーの扱い等)。CLI/TUI の更新で壊れうる保守リスク。
  → 完了判定は report ファイルに寄せ、TUI ストリーム解析は最小化。submit は別キー送出に固定。
- ~~現状コードはまだ self-loop/常駐 worker モデル。本決定の実装は follow-up~~ → **実装済み**(下記)。

## 実装メモ(2026-06-09 追記)

`InteractiveSessionRunner`(pywinpty)を `orchestrate.py` に実装し、`make_runner_for(codex/claude)` で差し替え。
1セッションを使い回し(warmup は1回)、各ステージは directive を注入。完了は report ファイルで判定。

### エンジン別 起動フラグ(実機で確定)
- **codex**: `-c service_tier=flex -c model_reasoning_effort=low --dangerously-bypass-approvals-and-sandbox`
  - ネストした pseudo-console 内では codex の windows-sandbox が PowerShell/node_repl を spawn できず
    **`windows sandbox: spawn setup refresh` で失敗**する。bypass で回避(orchestrator は信頼コード、
    worker は queue/ 内の read/write のみ=write-execute プレーン内)。
- **claude**: `--dangerously-skip-permissions`
  - **BypassPermissions の承認は初回一度きり**(`~/.claude.json` に記録)。一度手動で「Yes, I accept」すれば
    以降は driven セッションでもプロンプトが出ない。
  - `--permission-mode acceptEdits` は Write は無確認だが **rename 等のシェルコマンドが承認待ちで停止**するため不可。
- **directive は `.tmp→rename`(atomic)**。orchestrator の途中読みを原理的に防ぐ。上記フラグで両エンジンとも
  rename が無確認に通る。run() 側にも parse 失敗時の再読込ガードを保持。

### 検証
- spike(`tools/conpty_spike.py claude`)を **skip-permissions** で再実行 → step1/step2 とも PASS(承認プロンプト無しを確認)。
- フル pipeline: codex は `--engine codex` で実ビーム力学仮説を生成して完走。claude は `--engine claude` で end-to-end 確認。
- **テスト注記**: codex/claude は自前の tool 子プロセスを spawn するため、検証は PowerShell ツールの
  `dangerouslyDisableSandbox` 下で実行した(ネスト sandbox が二重になるのを避けるため)。

### 撤去
- 旧モデルのファイルを撤去: `tools/start-worker.ps1` / `tools/wait-for-task.ps1` / `tools/mock_worker.py` /
  `worker/INSTRUCTIONS.md`。heartbeat / `.alive` も廃止。`MockRunner` は `orchestrate.py` 内に in-process 実装。
- `python orchestrate.py` だけで spawn〜駆動〜完了まで完全自動(手動 worker タブ不要)。
