# claude-codex-orchestrator

研究アイディアの種を、複数の検証可能な仮説・反証・出典付きレポートに整理する研究支援ツール。

## これは何か

`claude-codex-orchestrator` は、曖昧な問いや研究の種を受け取り、複数の候補仮説に広げます。その後、弱点の洗い出し、文献・出典候補の収集、簡易検証、読みやすいレポート化までを1つの run として実行します。

AI 同士に正解を決めさせるツールではありません。Claude / Codex などの LLM は候補を出すために使い、検証結果・出典・判断材料は artifact としてファイルに残します。最終判断は人間が行います。

## 何ができるか

| できること | まだできないこと |
|---|---|
| 研究の種から複数の仮説を出す | 研究の正しさを証明する |
| 反証・ツッコミを出す | AI の合議で採用判断する |
| 文献・出典候補を集める | 実験や本格シミュレーションを自動実行する |
| 読みやすい `REPORT.md` を生成する | 人間の最終判断を代替する |
| fallback や検索品質を見える化する | 失敗した LLM 出力を自動で信用する |

## いつ使うか

- 研究テーマになりそうな問いを広げたい
- 複数の仮説を比較して、次に検証する順番を決めたい
- ある仮説の弱点、反証方法、近い先行研究を知りたい
- 宇宙機など特定ドメイン向けに研究案を出したい
- LLM が出した案を、そのまま信じずに artifact として監査したい

## システム全体像

```text
Seed / question
  -> Planner        目的・制約・評価軸を固定
  -> Generate       複数の候補を独立生成
  -> Proximity      重複や似た方向を整理
  -> Red-team       弱点・反証・交絡を出す
  -> Revise         指摘を受けて候補を改訂
  -> Verify         文献・形式・筋の良さを確認
  -> Hard gate      客観的にダメなものだけ落とす
  -> Arbiter        勝者を決めず、人間向けに整理
  -> REPORT         次に読む・検証する候補を提示
  -> Human decision 最終判断
```

詳しい設計思想、各コンポーネントの責務、安全境界は [ARCHITECTURE.md](./ARCHITECTURE.md) を参照してください。

## 最初の実行

まずは LLM を呼ばない `mock` で配管を確認します。

```powershell
python orchestrate.py --engine mock --seed "低軌道での宇宙機において表面物性、形状とCD値の関係性は?" --no-lit-search
```

成功すると `runs/<id>/` が作られます。最初に読むのは `runs/<id>/REPORT.md` です。

## 本番実行

既定では `dual` engine です。Codex と Claude Code の両方が使える場合、複数の発想レンズ(案を出すときの見方・切り口)を engine に割り当てて候補を生成します。

```powershell
python orchestrate.py --seed "ミューオン g-2 の残差を説明する新しい系統誤差源は?"
```

宇宙機向けの研究アイディアでは、専用 config を使います。

```powershell
python orchestrate.py --config configs/spacecraft.json --seed "低軌道での宇宙機において表面物性、形状とCD値の関係性は?"
```

制約を明示したい場合は `--constraints` を足します。

```powershell
python orchestrate.py --seed "..." --constraints "公開データのみ。新規実験は不可。手元PCで試せる範囲。"
```

## 主なオプション

| オプション | 意味 | 使う場面 |
|---|---|---|
| `--engine mock` | LLM なしで配管確認 | 初回テスト、CI 的な確認 |
| `--engine codex` | Codex のみ使う | 単一 engine で確認 |
| `--engine claude` | Claude のみ使う | 単一 engine で確認 |
| `--config configs/spacecraft.json` | 宇宙機向け設定で実行 | 宇宙機研究 |
| `--budget quick` | 少ない候補数で速く回す | 試行錯誤 |
| `--no-lit-search` | 文献検索を切る | offline / 高速確認 |
| `--n-lenses 6` | 使う発想レンズ数を増やす | 広く探索 |
| `--timeout 900` | 1 job の待ち時間を延ばす | LLM 応答が遅いとき |

## 実行後に何を見るか

1. `REPORT.md`
   最初に読むサマリ。結論、次に検証する順番、育てる順、注意点を見る。

2. `candidate_reports.md`
   候補ごとの詳細。一言で、なぜ面白いか、最初に潰す方法、反証されたらどうなるかを見る。

3. `decision_matrix.md`
   評価軸ごとの整理。勝者は選ばない。人間が比較するための表。

4. `priority.json`
   次に検証する順番の正本。決定的計算で作る。採用判定ではない。

5. `research_priority.json`
   LLM 推奨・要確認の「育てる順」。採用判定ではない。

6. `evidence/*.json`
   文献・出典検索の生データ。検索品質を確認する。

7. `fallbacks.json`
   LLM job の失敗・timeout・出力不備を別手段で補って完走したかの記録。`count` が 0 なら通常どおり。`count > 0` なら一部の結果を補っているので注意して読む。`REPORT.md` 冒頭にも警告が出る。

## 出力ディレクトリ

主要 artifact は次の通りです。

```text
runs/<id>/
  REPORT.md              最初に読む summary
  candidate_reports.md   候補ごとの人間向け詳細レポート
  decision_matrix.md     評価軸ごとの比較表
  priority.json          次に検証する順番
  research_priority.json LLM 推奨・要確認の育てる順
  charter.json           seed、制約、評価軸、発想レンズ
  candidates/*.json      生成候補
  reviews/*.json         red-team 指摘
  revised/*.json         改訂版
  verdicts/*.json        Tier0 検証結果
  evidence/*.json        文献・出典検索の生データ
  fallbacks.json         LLM job の失敗・timeout・出力不備を補って完走したかの記録(常に生成。count=0 なら通常どおり)
  control/               operator steering の note と適用 trace
  log/                   LLM 呼び出しログと session tail
```

## 重要な考え方

```text
AI は候補を出すだけ。
裁くのは客観検証。
決めるのは人間。
```

- AI debate は evidence ではない
- `priority.json` は採用順ではなく、次に検証する順番
- `research_priority.json` は LLM 推奨・要確認
- `decision_matrix.md` は勝者を選ばない
- fallback が出た run は、一部の LLM job を別手段で補っているので、`REPORT.md` 冒頭と `fallbacks.json` を確認する

## 実行中に状態を見る

LLM セッションが止まっていないか、承認待ちになっていないかを別ターミナルで確認できます。

```powershell
python tools/watch_run.py
```

`STATE` が `active` ならその job の出力が増えています。`timeout_idle` や `proc_dead` が出た場合は `REPORT.md`、`fallbacks.json`、`log/*.session.txt` を確認してください。

## 実行中に方向修正を入れる

実行中の run に、人間の注意点を artifact として追記できます。active turn にキーストローク割り込みはせず、次の安全な LLM job 境界で prompt に反映されます。

```powershell
python orchestrate.py steer active "この観点を優先して確認" --scope verify
python orchestrate.py steer latest "次回は negative control design を強める" --scope next_round
```

steering note は evidence ではありません。注意を向けるための情報であり、仮説の採用・棄却や evidence level には影響しません。

## Windows / engine の前提

- Python 3.9+ が必要です。
- Windows で Codex / Claude Code の対話セッションを駆動するには `pywinpty` が必要です。
- 現状、`codex` / `claude` engine の対話セッション駆動は Windows + pywinpty 前提です。Windows 以外では `--engine mock` で配管確認する想定です。
- 初回は次を実行します。

```powershell
pip install -r requirements.txt
```

- `codex` / `claude` がログイン済みで、PATH から起動できる必要があります。
- Claude Code は初回のみ `--dangerously-skip-permissions` の承認が必要です。
- Codex / Claude Code は headless ではなく、pywinpty 経由の対話セッションとして起動します。
- 起動時に必要な危険フラグは、ネスト環境で sandbox / permission prompt に詰まらないために orchestrator が付与します。Codex は `--dangerously-bypass-approvals-and-sandbox`、Claude Code は `--dangerously-skip-permissions` を使います。worker の書き込み対象は repo 内の queue / run artifact に限定する設計です。

## 詳細設定の補足

- 既定の engine は `dual` です。Codex と Claude Code の両方が解決できる場合は、発想レンズ(案を出すときの見方・切り口)を両 engine に割り当てます。
- `dual` では、候補の red-team / verify は原則として生成した engine と別 engine に割り当てます。これにより自己レビューを避けます。
- 現行の独立性は prompt-level です。同一 engine 内では常駐セッションを共有します(`session_scope=shared_engine_session`)。
- `stage_engine.proximity` / `stage_engine.research_priority` で、候補集合全体を見る job の engine を明示指定できます。空文字なら secondary engine を使います。
- `codex exec` や `claude -p` のような headless 実行は使いません。どちらも対話セッションとして起動します。
- `reasoning_effort` と `service_tier` は Codex の対話起動時に `-c service_tier=...` / `-c model_reasoning_effort=...` として渡します。
- `queue_poll_sec` と `queue_timeout_sec` は、LLM job の report を待つ間隔と上限時間です。`--timeout` で待ち時間を上書きできます。
- 文献検索は config の provider 設定に従います。高速確認や offline 確認では `--no-lit-search` を使います。
- Claude Code の初回 BypassPermissions 承認は手動で済ませておく必要があります。承認後は driven セッションで同じ確認が出ない前提です。

## ドメイン設定

既定設定は HEP / 加速器寄りです。宇宙機研究では `configs/spacecraft.json` を使います。

```powershell
python orchestrate.py --config configs/spacecraft.json --seed "宇宙機テレメトリの熱モデル残差で熱系異常を早期検知できるか"
```

宇宙機 config では arXiv と NASA NTRS を主な文献・出典 provider として使います。INSPIRE は放射線、検出器、プラズマなどの語が候補に含まれる場合の cross-domain hint として使います。ADS / TechPort 連携は future provider です。

ドメイン config で主に変わるもの:

- 発想レンズ(案を出すときの見方・切り口)
- 評価軸
- 文献・出典 provider
- red-team の追加チェック
- ideator へのドメイン指示

## メモリに記録する

run の結果を次回以降に反映したい場合だけ、人間が明示的に記録します。

```powershell
python orchestrate.py promote <run_id> <cand_id> --note "追求する理由"
python orchestrate.py reject  <run_id> <cand_id> --note "死んだ線の理由"
python orchestrate.py prefer  "高novelty優先 / 宇宙機の物理機構を重視"
```

run 終了時には `memory_suggestions.md` に記録候補が出ます。自動保存はされません。コマンドを実行した場合だけ memory に反映されます。

## トラブルシュート

| 症状 | 見るもの / 対処 |
|---|---|
| 何を読めばよいか分からない | まず `REPORT.md`、次に `candidate_reports.md` |
| engine が起動しない | `--engine mock` で配管確認。codex / claude の PATH とログイン状態を確認 |
| Windows で動かない | `pip install -r requirements.txt` で pywinpty を入れる |
| fallback 警告が出る | `REPORT.md` 冒頭、`fallbacks.json`、`candidate_reports.md` を確認 |
| 文献検索が弱い | `REPORT.md` の文献検索品質、`evidence/*.json`、config を確認 |
| LLM が止まって見える | `python tools/watch_run.py` と `log/*.session.txt` を確認 |

## まだやっていないこと

- 研究の正しさの証明
- 本格的な実験やフルシミュレーションの自動実行
- SPEC / PATCH ステージの本格実装
- 複数ラウンドの evolution loop
- ADS / TechPort / Semantic Scholar 連携(future provider)
- AI debate を evidence として扱うこと
- 人間の最終判断の代替

詳細な設計、不変条件、安全境界、artifact contract は [ARCHITECTURE.md](./ARCHITECTURE.md) を参照してください。
