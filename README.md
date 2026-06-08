# orchestration — IDEA-stage funnel MVP

研究の種を1件、**発散(独立) → red-team(攻撃→検証可能項目) → revise(1回改訂) → Tier0検証 → hard gate → arbiter(整理)**
で回し、`Research Hypothesis Contract` と `decision_matrix` を成果物として残す最小システム。

設計の全体像・思想・不変条件は **[ARCHITECTURE.md](./ARCHITECTURE.md)** を参照。このMVPは §11 の左下ループ1周。

> 原則: **AIは候補を出すだけ。裁くのは客観検証。決めるのは人間。** orchestrator(`orchestrate.py`)はLLMではない。

## 必要なもの
- Python 3.9+(標準ライブラリのみ。pip 不要)
- engine: **default は dual(Codex + Claude Code 両方)**。
  - **Codex**: `codex exec`(必須)。
  - **Claude Code**: **`claude -p` は使わない(別料金)**。`queue/` 経由で**常駐 Claude セッション**が処理(`claude-worker/INSTRUCTIONS.md`)。常駐 worker が居なければ自動で codex のみに degrade。

## 使い方

```bash
# まず配管をトークン無しで検証(推奨・最初の一回)
python orchestrate.py --engine mock --seed "テスト用の問い"

# 本番(codex)
python orchestrate.py \
  --seed "ミューオン g-2 の残差を説明する新しい系統誤差源は?" \
  --constraints "J-PARC E34 の既存データ。新規ビームタイムは不可。計算は手元GPU 1枚"

# レンズ数(=独立候補数)を変える
python orchestrate.py --seed "..." --n-lenses 6

# engine を明示(default は dual)
python orchestrate.py --seed "..." --engines codex          # codex のみ
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

## デュアルエンジン(Codex + Claude Code)
案の生成(generate)を **Codex と Claude Code 両方**で行うのが default(`engine: "dual"`)。レンズに engine を振り分ける。

- **Codex**: `codex exec` を直接呼ぶ。
- **Claude Code**: **`claude -p`(別料金)は使わない**。`queue/inbox/*.json` にタスクを書き、**別途起動した常駐 Claude セッション**が処理して `queue/reports/*.json` に返す(program_creater 方式)。起動手順は `claude-worker/INSTRUCTIONS.md`。
- **degrade / fallback**: 常駐 worker の heartbeat(`queue/claude.alive`)が無ければ claude を外して codex のみで実行。個別タスク失敗時も codex に自動フォールバック。**default が壊れない。**
- `decision_matrix.md` の `engine` 列・`REPORT.md` の「生成 engine 内訳」で、どちらが各候補を出したか分かる。
- redteam / revise / verify は primary(非claude)engine で実行(queue を溢れさせない)。

### Windows での実際の動かし方(2端末)
1. **新しい PowerShell** を2つ開く(Windows Terminal のタブ2つでよい)。両方で
   `cd C:\Users\hide\Documents\work\orchestration`(dual のコードがあるブランチに居ること)。
2. **端末B(worker を立てる)**: `.\tools\start-claude-worker.ps1`
   → `queue を見張っています` と出れば準備完了(対話セッション。`claude -p` 不使用 = 別料金なし)。
3. **端末A(オーケストレータ)**: `python orchestrate.py --seed "あなたの問い"`
   → `生成: N候補 (codex:.., claude:..)` で両エンジン稼働。
- worker を立てたら **すぐ**端末A を回す(heartbeat の鮮度で生存判定。既定 120 秒)。
- 終了は端末B を `Ctrl+C`。閉じれば次回は自動で codex のみ。

配管だけ試す(トークン/常駐 Claude 不要):
```bash
python tools/mock_claude_worker.py 60        # 別ターミナルで偽 worker(heartbeat + mock 応答)
python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
```

## 設定 (`config.json`)
- `engine`: `dual`(default=Codex+Claude) | `codex` | `claude` | `mock`。`engines` で dual の割当。
- `model`/`reasoning_effort`: MVP は速度優先で `gpt-5.5`/`low`。質を上げるなら `medium`+。
- `service_tier`: `flex`。**理由**: ユーザの `~/.codex/config.toml` が `service_tier = "default"` で
  CLI(v0.121)が設定読込ごと失敗するため、`-c service_tier=flex` で**毎回上書き**して回避している
  (グローバル設定は書き換えていない)。恒久対応するなら config.toml の3行目を `flex` に直すと
  `-c` 上書きが不要になる。
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
- **デュアルエンジン(Codex+Claude)実装済み**(claude は queue 経由、`claude -p` 不使用)。
  ベンダー多様性の追加価値(H2)は #12 のベンチで検証する。
- 文献検索は arXiv + INSPIRE-HEP を実装。Semantic Scholar/ADS 連携は未実装。
- 収束ループ(複数ラウンド)は未実装(現状 red-team 後の revise 1回のみ)。

## 既知の前提
- codex は **ChatGPT ログイン済み**であること(`codex login status` で確認)。
- worker は読み取り専用 sandbox(`-s read-only`)で実行。書き込み・実行は ARCHITECTURE §8 の
  Write/Execute プレーンに従って今後ゲートする。
