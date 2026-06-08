# orchestration — IDEA-stage funnel MVP

研究の種を1件、**発散(独立) → red-team(攻撃→検証可能項目) → revise(1回改訂) → Tier0検証 → hard gate → arbiter(整理)**
で回し、`Research Hypothesis Contract` と `decision_matrix` を成果物として残す最小システム。

設計の全体像・思想・不変条件は **[ARCHITECTURE.md](./ARCHITECTURE.md)** を参照。このMVPは §11 の左下ループ1周。

> 原則: **AIは候補を出すだけ。裁くのは客観検証。決めるのは人間。** orchestrator(`orchestrate.py`)はLLMではない。

## 必要なもの
- Python 3.9+(標準ライブラリのみ。pip 不要)
- engine: **codex CLI**(`codex exec`)。`claude` CLI があれば config で差し替え可。

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
```

出力は `runs/<timestamp>-<slug>/` に出る。まず `REPORT.md` と `decision_matrix.md` を読む。

```
runs/<id>/
├── REPORT.md            # 人間向けサマリ + 読み方 + 次の一手
├── decision_matrix.md   # 生存候補を評価軸ごとに一覧(勝者は選ばない)
├── decision_matrix.json
├── charter.json         # 固定した seed/制約/評価軸/レンズ
├── candidates/*.json    # 各 Research Hypothesis Contract
├── reviews/*.json       # red-team の攻撃(検証項目へ変換済み)
├── revised/*.json       # red-team を受けた改訂版(原案は candidates/ に残す)
├── evidence/*.json      # orchestrator が機械的に集めた候補文献など
├── verdicts/*.json      # Tier0 検証(novelty/soundness/feasibility + prior_art/evidence_refs)
├── discarded.md         # hard gate 落ち(理由付き・消さない)
├── unresolved.md        # 未解決論点 + 未追跡の stronger_variant
└── log/                 # 各LLM呼び出しの生ログ(provenance)
```

## 設定 (`config.json`)
- `engine`: `codex` | `claude` | `mock`
- `model`/`reasoning_effort`: MVP は速度優先で `gpt-5.5`/`low`。質を上げるなら `medium`+。
- `service_tier`: `flex`。**理由**: ユーザの `~/.codex/config.toml` が `service_tier = "default"` で
  CLI(v0.121)が設定読込ごと失敗するため、`-c service_tier=flex` で**毎回上書き**して回避している
  (グローバル設定は書き換えていない)。恒久対応するなら config.toml の3行目を `flex` に直すと
  `-c` 上書きが不要になる。
- `n_lenses`: 使う発散レンズ数(=独立候補数)。
- `concurrency`: 並列呼び出し数。
- `lit_search_enabled`: Tier0 novelty 補助として arXiv API から候補文献を取得し、
  `evidence/*.arxiv.json` に保存して Verifier に渡す。

## このMVPでまだやっていないこと(ARCHITECTURE §12)
- **Spec / Patch ステージ**(重心は IDEA なので未実装)。
- claude worker(CLI 未導入)。今は codex を複数レンズで回す=**戦略多様性**で代替。
  ベンダー多様性の追加価値(H2)は engine を足して比較する。
- 文献検索は arXiv API の候補取得のみ実装。INSPIRE-HEP/Semantic Scholar/ADS 連携は未実装。
- 収束ループ(複数ラウンド)は未実装(現状 red-team 後の revise 1回のみ)。

## 既知の前提
- codex は **ChatGPT ログイン済み**であること(`codex login status` で確認)。
- worker は読み取り専用 sandbox(`-s read-only`)で実行。書き込み・実行は ARCHITECTURE §8 の
  Write/Execute プレーンに従って今後ゲートする。
