# orchestration — IDEA-stage funnel MVP

研究の種を1件、**発散(独立) → red-team(攻撃→検証可能項目) → revise(1回改訂) → Tier0検証 → hard gate → arbiter(整理)**
で回し、`Research Hypothesis Contract` と `decision_matrix` を成果物として残す最小システム。

設計の全体像・思想・不変条件は **[ARCHITECTURE.md](./ARCHITECTURE.md)** を参照。このMVPは §11 の左下ループ1周。

> 原則: **AIは候補を出すだけ。裁くのは客観検証。決めるのは人間。** orchestrator(`orchestrate.py`)はLLMではない。

## 必要なもの
- Python 3.9+(標準ライブラリのみ。pip 不要)
- engine: **default は dual(Codex + Claude Code 両方)**。両方とも **headless は使わない**
  (`codex exec` / `claude -p` を呼ばない)。各 engine は **常駐セッション + ファイル queue** で動く:
  別ターミナルで `codex` / `claude` を対話起動し `queue/<engine>/` を処理させる(`worker/INSTRUCTIONS.md`)。
  worker が居ない engine は自動で除外。実機を回すには **最低1つ worker が必要**(配管確認は `--engine mock`)。

## 使い方

```bash
# まず配管をトークン無しで検証(推奨・最初の一回)
python orchestrate.py --engine mock --seed "テスト用の問い"

# 本番(dual = Codex + Claude)。先に別ターミナルで worker を立てる:
#   .\tools\start-worker.ps1 codex   と   .\tools\start-worker.ps1 claude
python orchestrate.py \
  --seed "ミューオン g-2 の残差を説明する新しい系統誤差源は?" \
  --constraints "J-PARC E34 の既存データ。新規ビームタイムは不可。計算は手元GPU 1枚"

# レンズ数(=独立候補数)を変える
python orchestrate.py --seed "..." --n-lenses 6

# engine を明示(default は dual)。指定した engine の worker が必要
python orchestrate.py --seed "..." --engines codex          # codex worker のみ
python orchestrate.py --seed "..." --engines codex,claude   # 両方(= default)
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
├── evidence/*.json      # arXiv(preprint)/INSPIRE-HEP(権威DB)から機械収集した候補文献
├── verdicts/*.json      # Tier0 検証(novelty/soundness/feasibility + prior_art/evidence_refs)
├── discarded.md         # 客観 hard gate 落ちのみ(形不備など。理由付き・消さない)
├── unresolved.md        # 未解決論点 + 未追跡の stronger_variant
└── log/                 # 各LLM呼び出しの生ログ(provenance)
```

## デュアルエンジン(Codex + Claude Code)— 完全統一
**codex も claude も headless(codex exec / claude -p)を使わず、両方とも「常駐セッション + ファイル queue」に統一。**
orchestrator は engine を直接呼ばず、`queue/<engine>/inbox` に依頼を書いて `queue/<engine>/reports` を待つだけ(program_creater / Shogun 方式)。

- **全ステージが queue 経由**: generate はレンズに engine を振り分け、redteam / revise / verify は primary(先頭の生存 engine)が処理。
- **worker**: 別途 `codex` / `claude` を対話起動し queue を処理(`worker/INSTRUCTIONS.md`、起動は `tools/start-worker.ps1 <engine>`)。
- **生存判定 / degrade**: `queue/<engine>.alive` の heartbeat が古い engine は自動で除外。生存 engine が無ければ「worker を立てて」と表示して終了(`--engine mock` は worker 不要)。タスク失敗時は別の生存 engine にフォールバック。
- **可視化**: `decision_matrix.md` の `engine` 列・`REPORT.md` の「生成 engine 内訳」で、どちらが各候補を出したか分かる。

### Windows での実際の動かし方(端末を分ける)
1. **新しい PowerShell** を engine 数+1 だけ開く(Windows Terminal のタブでよい)。各タブで
   `cd C:\Users\hide\Documents\work\orchestration`(dual のコードがあるブランチに居ること)。
2. **worker を立てる**(engine ごとに1タブ):
   ```powershell
   .\tools\start-worker.ps1 codex
   .\tools\start-worker.ps1 claude
   ```
   各 `<engine> queue を見張っています` と出れば準備完了(対話セッション。headless 不使用)。
3. **オーケストレータ**(別タブ): `python orchestrate.py --seed "あなたの問い"`
   → `生成: N候補 (codex:.., claude:..)` で両エンジン稼働。
- worker を立てたら **すぐ**(既定120秒以内)オーケストレータを回す(heartbeat の鮮度で生存判定)。
- 終了は各 worker タブを `Ctrl+C`。止めた engine は次回 run から自動で外れる。

配管だけ試す(トークン/常駐セッション不要):
```bash
python tools/mock_worker.py claude 120       # 別ターミナルで偽 worker(heartbeat + mock 応答)
python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
```

## 設定 (`config.json`)
- `engine`: `dual`(default=Codex+Claude) | `codex` | `claude` | `mock`。`engines` で dual の割当。
- `model`/`reasoning_effort`/`service_tier`: orchestrator は engine を直接呼ばないため**未使用**
  (model 等は各常駐セッション側で選ぶ)。`service_tier=flex` は codex worker 起動スクリプトが
  `~/.codex/config.toml` の不正な `service_tier="default"` を回避するために使う。
- `queue_poll_sec`/`queue_timeout_sec`/`heartbeat_stale_sec`: queue ポーリング間隔 / 応答待ち上限 / worker を生存とみなす鮮度(秒)。
- `n_lenses`: 使う発散レンズ数(=独立候補数)。
- `concurrency`: 並列呼び出し数。
- `lit_search_enabled` / `inspire_enabled`: Tier0 novelty 補助として arXiv(preprint)と
  INSPIRE-HEP(権威DB)から候補文献を取得し `evidence/*.{arxiv,inspire}.json` に保存して Verifier に渡す。
  `--no-lit-search` で一括無効化(offline/高速テスト用)。
- **検証ゲートの方針**: 自動 reject は**客観基準(形不備など)のみ**。LLM の `kill` は推奨扱いで落とさず、
  `decision_matrix` に `kill?(LLM/要確認)` として残す(誤kill救済 / ARCHITECTURE §3.6)。

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
- **デュアルエンジン(Codex+Claude)実装済み・完全統一**(両方とも headless 不使用、常駐+queue)。
  ベンダー多様性の追加価値(H2)は #12 のベンチで検証する。
- 文献検索は arXiv + INSPIRE-HEP を実装。Semantic Scholar/ADS 連携は未実装。
- 収束ループ(複数ラウンド)は未実装(現状 red-team 後の revise 1回のみ)。

## 既知の前提
- codex / claude が **ログイン済み**で、新しいターミナルの PATH から対話起動できること。
- 実機 run には **対象 engine の常駐 worker が必要**(無ければその engine は自動で外れ、生存ゼロなら終了)。
- worker は `queue/<engine>/` 内のみ読み書きする(ARCHITECTURE §8 の Write/Execute プレーンに従う)。
