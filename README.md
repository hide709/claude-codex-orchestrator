# orchestration — IDEA-stage funnel MVP

研究の種を1件、**発散(独立) → proximity(重複検知・多様性/注釈のみ) → red-team(攻撃→検証可能項目) → revise(1回改訂) → Tier0検証 → hard gate → arbiter(整理)**
で回し、`Research Hypothesis Contract` と `decision_matrix` を成果物として残す最小システム。

設計の全体像・思想・不変条件は **[ARCHITECTURE.md](./ARCHITECTURE.md)** を参照。このMVPは §11 の左下ループ1周。

> 原則: **AIは候補を出すだけ。裁くのは客観検証。決めるのは人間。** orchestrator(`orchestrate.py`)はLLMではない。

## 必要なもの
- Python 3.9+ と **pywinpty**(`pip install -r requirements.txt`。Windows 専用)。
- engine: **default は dual(Codex + Claude Code 両方)**。両方とも **headless を使わない**
  (`codex exec` / `claude -p` を呼ばない = サブスク内・**追加課金なし**)。orchestrator が **pywinpty(ConPTY)で
  対話セッションを自動 spawn・駆動**する(ADR-001)。**手動で worker を立てる必要はない。**
  実行ファイルを解決できない engine は自動で除外(配管確認は `--engine mock`、pywinpty 不要)。

## 使い方

```bash
# まず配管をトークン無しで検証(推奨・最初の一回)
python orchestrate.py --engine mock --seed "テスト用の問い"

# 本番(dual = Codex + Claude)。orchestrator が両セッションを pywinpty で自動起動・駆動する
python orchestrate.py \
  --seed "ミューオン g-2 の残差を説明する新しい系統誤差源は?" \
  --constraints "J-PARC E34 の既存データ。新規ビームタイムは不可。計算は手元GPU 1枚"

# レンズ数(=独立候補数)を変える
python orchestrate.py --seed "..." --n-lenses 6

# engine を明示(default は dual)
python orchestrate.py --seed "..." --engine codex           # codex のみ
python orchestrate.py --seed "..." --engine claude          # claude のみ

# ドメインを切り替える(例: 宇宙機研究)
python orchestrate.py --config configs/spacecraft.json --seed "..."
```

出力は `runs/<timestamp>-<slug>/` に出る。まず `REPORT.md` と `decision_matrix.md` を読む。

```
runs/<id>/
├── REPORT.md            # 人間向けサマリ + 読み方 + 次の一手
├── decision_matrix.md   # 生存候補を軸ごとに一覧(勝者は選ばない / kill?(LLM)=要人間確認)
├── decision_matrix.json
├── charter.json         # 固定した seed/制約/評価軸/レンズ
├── candidates/*.json    # 各 Research Hypothesis Contract
├── reviews/*.json       # red-team の攻撃(検証項目へ変換済み)
├── revised/*.json       # red-team を受けた改訂版(原案は candidates/ に残す)
├── evidence/*.json      # arXiv(preprint)/INSPIRE-HEP/NASA NTRS(権威DB)から機械収集した候補文献
├── verdicts/*.json      # Tier0 検証(novelty/soundness/feasibility + prior_art/evidence_refs)
├── proximity.json       # within-run の重複クラスタ・多様性・未探索軸(#34。注釈のみ・棄却しない)
├── hypothesis_graph.json# 仮説 lineage(seed→生成→攻撃→改訂→検証 の typed edges / #35)+ .md
├── discarded.md         # 客観 hard gate 落ちのみ(形不備など。理由付き・消さない)
├── unresolved.md        # 未解決論点 + 未追跡の stronger_variant
└── log/                 # 各LLM呼び出しの生ログ(provenance)
```

## デュアルエンジン(Codex + Claude Code)— 完全統一
**codex も claude も headless(codex exec / claude -p)を使わず、両方とも対話セッションに統一**(サブスク内・追加課金なし)。
orchestrator が **pywinpty(ConPTY)で対話セッションを spawn し、job 単位で directive を注入 → `queue/<engine>/reports/<label>.json` で完了検知**(ADR-001)。
LLM に常駐ループは持たせない(**supervisor がループを所有**)。1セッションを使い回す(warmup は1回、各ステージは注入)。

- **全ステージが同一セッション経由**: generate はレンズに engine を振り分け、redteam / revise / verify は primary(先頭の生存 engine)が処理。
- **起動フラグ(自動付与)**: codex=`--dangerously-bypass-approvals-and-sandbox`(ネスト環境の sandbox spawn 失敗を回避)、claude=`--dangerously-skip-permissions`。どちらも write-execute は repo 内に限定(§8)。
- **生存判定 / degrade**: 実行ファイルを解決できない engine は自動で除外。生存 engine が無ければ終了(`--engine mock` は pywinpty 不要)。タスク失敗時は別の生存 engine にフォールバック。
- **可視化**: `decision_matrix.md` の `engine` 列・`REPORT.md` の「生成 engine 内訳」で、どちらが各候補を出したか分かる。

### Windows での実際の動かし方
```powershell
pip install -r requirements.txt             # 初回のみ(pywinpty)
python orchestrate.py --seed "あなたの問い"     # これだけ。両セッションは自動で起動・駆動される
```
→ `生成: N候補 (codex:.., claude:..)` で両エンジン稼働。**別タブで worker を立てる必要はない。**

前提(初回のみ):codex / claude が**ログイン済み**で、claude は一度だけ手動で `--dangerously-skip-permissions` の
「Yes, I accept」を承認しておく(`~/.claude.json` に記録され、以降 driven セッションでプロンプトは出ない)。

配管だけ試す(pywinpty / トークン不要):
```bash
python orchestrate.py --engine mock --no-lit-search --seed "queue test"
```

## 設定 (`config.json`)
- `engine`: `dual`(default=Codex+Claude) | `codex` | `claude` | `mock`。`engines` で dual の割当。
- `reasoning_effort`/`service_tier`: **codex の対話起動フラグに渡す**(`-c service_tier=flex -c model_reasoning_effort=low`)。`model` は各セッション側で選ぶため未使用。
- `queue_poll_sec`/`queue_timeout_sec`: report ポーリング間隔 / 1 job の応答待ち上限(秒。`--timeout` で上書き)。
- `session_warmup_sec`/`inject_enter_delay_sec`: セッション起動待ち / 注入時に Enter を別送するまでの遅延(秒)。
- `n_lenses`: 使う発散レンズ数(=独立候補数)。
- `concurrency`: 並列呼び出し数。
- `lit_search_enabled` / `inspire_enabled`: Tier0 novelty 補助として arXiv(preprint)と
  INSPIRE-HEP(権威DB)から候補文献を取得し `evidence/*.{arxiv,inspire}.json` に保存して Verifier に渡す。
  `--no-lit-search` で一括無効化(offline/高速テスト用)。
- **検証ゲートの方針**: 自動 reject は**客観基準(形不備など)のみ**。LLM の `kill` は推奨扱いで落とさず、
  `decision_matrix` に `kill?(LLM/要確認)` として残す(誤kill救済 / ARCHITECTURE §3.6)。

## ドメイン設定(domain config / Issue #40)
既定(`config.json`)は HEP/加速器向け。**workflow は変えず**、config だけでドメインを差し替える:

```bash
python orchestrate.py --config configs/spacecraft.json --seed "宇宙機テレメトリの熱モデル残差で熱系異常を早期検知できるか"
```

domain config で差し替わるもの(`configs/spacecraft.json` が実例):
- `lenses` + `lens_desc` … 発散レンズと説明(spacecraft: telemetry_and_diagnostics / physics_mechanism / modeling_and_simulation / validation_strategy / prior_art_difference)
- `eval_axes` … decision_matrix / verdict の評価軸。既定4軸に **mechanism_clarity / validation_clarity / baseline_clarity** を追加(軸は増やしても単一スコアには潰さない)
- evidence providers … spacecraft は **arXiv + NASA NTRS** が primary。**INSPIRE は `inspire_mode: "trigger"`**(放射線/検出器/プラズマ等の語が候補に含まれる時だけ cross-domain hint として検索)。ADS/TechPort は未実装(後回し)
- `redteam_extra_checks` … red-team の追加観点。**`too_close_to_product_development`(研究仮説ではなく開発改善案に寄りすぎ)** が spacecraft の要
- `seed_charter_note` … ideator への域内指示(「開発案でなく研究の種を出す」等)

contract には任意フィールド `baseline` / `success_metric` / `failure_condition` を追加(全分野で有効、未記入でも形ゲートは通る)。

## メモリ(cross-run / Issue #23)
run をまたいで「却下した線・採用した方向・好み」を覚え、**次回の生成と検証に反映**する。
永続するのは **人間が記録した知識だけ**(`memory/`)。`runs/` は使い捨てのまま。

```bash
python orchestrate.py promote <run_id> <cand_id> --note "追求する理由"
python orchestrate.py reject  <run_id> <cand_id> --note "死んだ線の理由"
python orchestrate.py prefer  "高novelty優先 / ビームダイナミクス系統に注力"
```

- `memory/decisions.jsonl`(commit)= 採用/却下、`memory/preferences.md`(commit)= 好み、
  `memory/seen.jsonl`(gitignore)= 重複検知用の自動キャッシュ。
- 効き方: 却下済みは**再提案しない** / 好みに寄せる / 既出と重複する候補は `REPORT.md` で印(**検知のみ・棄却しない**)。
- 原則は ARCHITECTURE §0(AI判断で自動棄却しない)。詳細は `memory/README.md`。

## このMVPでまだやっていないこと(ARCHITECTURE §12)
- **Spec / Patch ステージ**(重心は IDEA なので未実装)。
- **デュアルエンジン(Codex+Claude)実装済み・完全統一**(両方とも headless 不使用、pywinpty 駆動の対話セッション)。
  ベンダー多様性の追加価値(H2)は #12 のベンチで検証する。
- 文献検索は arXiv + INSPIRE-HEP を実装。Semantic Scholar/ADS 連携は未実装。
- 収束ループ(複数ラウンド)は未実装(現状 red-team 後の revise 1回のみ)。

## 既知の前提
- **pywinpty** が入っていること(`pip install -r requirements.txt`、Windows 専用)。
- codex / claude が **ログイン済み**で PATH から起動できること(claude は初回のみ BypassPermissions を手動承認)。
- driven セッションは `queue/<engine>/` 内のみ読み書きする想定(ARCHITECTURE §8 の Write/Execute プレーン)。
- 実行ファイルを解決できない engine は自動で外れ、生存ゼロなら終了(`--engine mock` は pywinpty 不要)。
