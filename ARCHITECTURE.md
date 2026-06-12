# ARCHITECTURE — Claude/Codex 候補生成 + 客観検証 + 人間レビュー システム

研究の初期アイディアだしを主目的に、開発支援(実装)まで同じ骨格でつなぐマルチエージェント設計。
**Claude/Codex のペアプロでも、AI同士の合議でもない。** AI は候補を出すだけ。裁くのは客観検証、決めるのは人間。

この文書は、実装や設計を変える人のための詳細設計です。初めて使う人は、先に [README.md](./README.md) を読んでください。

## この文書の読み方

- まず §0 の原則を読む。ここが設計判断の基準。
- 次に §1 の全体フローと §3 のコンポーネント責務を読む。
- 実装時は §6 の不変条件と §8 の安全境界を確認する。
- 用語が分からない場合は末尾の §13 用語集を見る。

原則・禁止事項・§番号は、issue、PR review、コードコメントから参照される。読みやすくするための言い換えはよいが、意味を変える場合は PR 本文に before/after の差分を明示する。

## 設計要約

このシステムは、LLM を「正解を出す存在」ではなく「仮説生成器」として使う。生成された候補は、red-team、文献検索、簡易検証、hard gate、人間レビューを通して扱う。

判断はチャットではなく artifact で行う。候補、反証、検証結果、出典、fallback 記録をファイルとして残し、人間が最終判断できる形に整理する。

---

## 0. 設計思想（最初に固定する原則）

1. **AI同士に「どちらが正しいか」を聞かない。** AI は候補・仮説・patch・test・review・risk を出す。正しさ/採用可否は、test / lint / typecheck / CI / 文献 / 計算 / 実験 / 人間レビューが判定する。
2. **連携は成果物(artifact)経由。自然言語チャットで合議しない。**
3. **AI の合意を merge 条件にしない。** 合意は意味のある信号ではない。
4. **最終判断は Human が持つ。**
5. **重心は「実装」より「研究の初期段階」。** 曖昧なアイディアを「検証可能な仮説」と「最小実験」に変換することを最重要視する。

> この5つは load-bearing。以降の設計は全部この帰結。迷ったら 0 に戻る。

なぜ必要か:

- LLM 同士の合意は、同じ誤りを共有している可能性がある。
- 研究アイディアは、議論で強く見える順に正しいとは限らない。
- そのため、AI は候補生成と反証生成に使い、採用・棄却の判断は artifact、客観検証、人間レビューへ渡す。

---

## 1. 全体フロー

短く書くと、現在の IDEA-stage pipeline は次の形。

```text
Planner -> Generate(engine x 発想レンズ) -> Proximity -> Red-team -> Revise -> Verify -> Hard gate -> Arbiter -> Reports -> Human
```

詳細な責務と成果物の流れは下図の通り。

```
┌─────────┐
│ HUMAN   │  seed / 制約 / steering note
└────┬────┘
     ▼
┌──────────────┐
│ PLANNER      │  `charter.json` に目的・制約・評価軸・発想レンズを固定
└────┬─────────┘
     ▼
┌──────────────────────────────────────────────┐
│ GENERATE                                     │
│ engine(codex/claude/mock) x 発想レンズ        │
│ 発想レンズ = 案を出すときの見方・切り口        │
│ 実行基盤 = ADR-001 ConPTY session + queue     │
│ 生成時は他候補を見せない                       │
└────┬─────────────────────────────────────────┘
     ▼  `candidates/*.json`
┌──────────────┐
│ PROXIMITY    │  重複・近い候補・未探索方向を注釈する。棄却には使わない
└────┬─────────┘
     ▼  `proximity.json`
┌──────────────┐
│ RED-TEAM     │  弱点・交絡・反証案を出す。judge しない
└────┬─────────┘
     ▼  `reviews/*.json`
┌──────────────┐
│ REVISE       │  red-team 指摘を受けて1回だけ改訂。原案は残す
└────┬─────────┘
     ▼  `revised/*.json`
┌──────────────┐
│ VERIFY       │  候補ごとに文献・形式・筋の良さ・実行しやすさを確認
└────┬─────────┘
     ▼  `verdicts/*.json` + `evidence/*.json`
╔══════════════╗
║ HARD GATE    ║  自動棄却は形不備など客観的な不備のみ
║              ║  LLM の kill は `kill?(LLM/要確認)` として残す
╚════╤═════════╝
     ├─ 形不備のみ → `discarded.md`
     ▼
┌──────────────┐
│ ARBITER      │  決定的 Python。勝者を選ばず、人間が判断しやすい表に整理
└────┬─────────┘
     ▼
`decision_matrix.*` / `priority.json`
     │
     ├─ optional LLM: RESEARCH PRIORITY REVIEWER
     │      → `research_priority.json`(LLM 推奨・要確認。採用判定ではない)
     ▼
`REPORT.md` / `candidate_reports.md` / `memory_suggestions.*`
     ▼
┌─────────┐
│ HUMAN   │  採用 / 棄却 / 次に検証する候補への予算配分
└─────────┘

全ての受け渡しは Artifact Bus(ファイル)経由。自然言語チャットで合議しない。
全成果物に provenance(worker/model/seed/commit)を刻印する。
```

---

## 2. 3段ファネル（同じ骨格が段階で特殊化する）

コミットが深まるほど、**検証者の正体が「人間判断寄り」→「完全客観」へ移る。** だから単一エンジンでは作らない。

| | STAGE 1 **IDEA**(研究の種) | STAGE 2 **SPEC** | STAGE 3 **PATCH** |
|---|---|---|---|
| 目的 | 検証可能な仮説に加工する | MVP範囲/受入条件を確定 | 動く実装を選ぶ |
| 検証者 | **低コスト確認から高コスト確認へ進むラダー**(§4) | acceptance の検証可能性(半客観) | test/lint/CI/diff(客観・低コスト・並列) |
| 性質 | 人間中心ファネル / 候補少・検証に費用と時間がかかる | 軽量(Planner+人間承認で足りる) | 本物の並列トーナメント |
| AI の効き所 | **ここに重心** | 補助 | 候補を多く出し、機械で棄却しやすい |
| トーナメント成立 | ❌(ファネルとして作る) | △ | ✅ |

**注意:** STAGE 1 を STAGE 3 と同じ「並列トーナメント」として実装してはいけない。検証コストの非対称性が逆転している(idea は生成の手間が小さく、検証に費用と時間がかかる)。

---

## 3. コンポーネント責務

各コンポーネントは「責務 / 入力 / 出力 / 禁止事項」で定義する。**禁止事項が設計の本体**(原則 §0 を局所化したもの)。

読むときは、各コンポーネントを次の観点で見る:

- 役割: 何を担当するか
- 入力: 何を読むか
- 出力: どの artifact を残すか
- 禁止: 何をしてはいけないか
- 判断権: どこまで決めてよいか

### 3.1 HUMAN
- **責務:** 種/問い/タスクと制約の投入。最終採用・merge・方向転換。**idea段階では「どの残った候補に実験予算を割くか」の配分。**
- **決定権:** ◎ 全部。

### 3.2 PLANNER
- **責務:** 目的分解、成功条件、触ってよい範囲、禁止操作、評価軸、**hard gate の定義**を `charter.json` に固定(以後ロック)。
- **入力:** Human の種+制約。
- **出力:** `charter.json`。
- **禁止:** 自分で案を出さない(隠れ worker 化の禁止)。gate を曖昧にしない。

### 3.3 WORKERS（独立生成 / engine x 発想レンズ）
- **責務:** charter を受け、**互いを見ずに** 候補を生成。
  - **engine:** `mock` / `codex` / `claude`。現行実装では同じ生成タスクを engine に割り当てる。
  - **発想レンズ(lens):** 案を出すときの見方・切り口。例: analogy、anomaly、method-driven、contrarian、gap、combination。
  - **role 特化:** Claude=文献、Codex=計算のような役割固定は未実装。将来の worker 設定(#3/#59)で扱う。
- **入力:** `charter.json`。
- **出力:** `candidates/*.json`(Research Hypothesis Contract)。
- **実行基盤:** ADR-001 の ConPTY(pywinpty) session + queue/report protocol。worktree 隔離は現行実装ではない。
- **禁止:** 生成段階で他 worker の案を参照しない(独立性=多様性の源)。
- **独立性の範囲(#59):** 現行 v1 は `prompt-level independent`。兄弟候補は prompt には渡さないが、同一 engine 内では `shared_engine_session` のため session-level では履歴を共有する。完全な `fresh_session_per_candidate` は #12/#42 の実測後に検討する future option。

> ベンダー多様性(Claude vs Codex)より、**戦略多様性(発想レンズの違い)**の方が候補の質に効く。
> 「単一モデル+複数戦略プロンプト」でも多様性は出る。ベンダー混在の追加価値は §11 の H2 で検証する。

### 3.3b PROXIMITY（within-run 重複検知・多様性 / Issue #34）
- **責務:** GENERATE 直後に候補集合を俯瞰し、同型(言い換え)クラスタ・多様性警告・未探索軸を**注釈**する(Co-Scientist の Proximity 相当)。
- **機構:** クラスタ所属は**決定的**(char-bigram Jaccard + union-find)。LLM は theme / diversity_warning / underexplored_axes の**ラベル付けのみ**(所属を変更しない。失敗しても決定的注釈は残る)。
- **出力:** `proximity.json`、各候補に `_cluster_id`、decision_matrix の cluster 列、REPORT の多様性セクション。
- **禁止:** **これを理由に棄却しない**(全メンバーが red-team / verify を受ける)。representative は表示・整理用であり、予算配分に使うのは #37 の責務。cross-run 重複検知(memory/seen)とは別物。
- **engine 割当(#59):** 候補集合全体を見る job なので cross-engine 原則の対象外。`stage_engine.proximity` で明示指定でき、空文字なら secondary engine を使う。
- **lineage(Issue #35):** 各候補は `_lineage`(hypothesis_id / generation_round / parents / operator / cluster_id / 自己申告の changes・resolved_red_team_issues)を持ち、run 終端で `hypothesis_graph.{json,md}`(seed→生成→攻撃→改訂→検証の typed edges)に決定的に集約される。**graph は data model であり、実行は linear pipeline のまま**(scheduler の導入判断は Issue #42)。

### 3.4 RED-TEAM（cross-review を付け替えたもの）
- **責務:** 他 worker の案を **攻撃して鋭くする**。隠れ前提、交絡、反例、feasibility 穴、強い隣接変種を出す。
- **入力:** 候補成果物(著者は伏せる=blind が望ましい)。
- **出力:** `attacks[]`。各攻撃は **`convert_to`** で検証可能項目に変換される(§7.2)。変換できない攻撃は捨てる。
- **禁止:**
  - **judge しない。** 「良い/悪い」「どちらが勝ち」「スコア」を出さない(これは VERIFIER と HUMAN の領分)。
  - **均質化させない。** 案をマージ/合意へ寄せる圧をかけない。攻撃されたら原案を改訂(justified diff)するか、原案と改訂版を2候補として残す。
- **位置づけ:** critic は「反証仮説を生成」するだけ。正しいかは VERIFIER が客観確認する(critique 自体に generate-and-verify を再帰)。critic は VERIFIER の recall を上げる役で、裁判官ではない。
- **engine 割当(#59):** dual では候補を生成した engine と別 engine が red-team する。単一 engine のときは従来どおり同じ engine で処理する。

### 3.5 VERIFIER
- **責務:** **候補ごとに・客観的に** 検証し、証拠(evidence)と verdict を出す。
  - 現行 IDEA-stage: 形式、文献候補、筋の良さ(soundness)、実行しやすさ(feasibility)を確認する。
  - 将来 PATCH-stage(future #21): test / lint / typecheck / CI / diff-guard。
- **入力:** 候補 + red-team が出した検証可能項目。
- **出力:** `verdicts/*.json` + `evidence/*.json`。
- **禁止:**
  - **候補同士を比較しない**(横断は ARBITER の仕事)。
  - **「良し悪し」を主観判定しない。** 測れる事実だけを出す。
  - **生成者と同一であってはならない**(Codex worker が自分用の Verifier/gate を設計するのは利益相反。gate は PLANNER/人間が事前固定)。
- **engine 割当(#59):** dual では候補を生成した engine と別 engine が verify する。実際の検証 engine は `verdicts/*.json` の provenance と `candidate_reports.md` に残す。
- **重要(研究の新規性):** 文献チェックは AI の記憶でやらない。現行 provider は **arXiv / INSPIRE-HEP / NASA NTRS**。ADS / TechPort / Semantic Scholar / 一般ウェブ検索は future provider。**出典を tier 付けして evidence に記録**する:
    - 権威DB(INSPIRE等のcurated DB) > 査読論文 > preprint(arXiv等) > web/blog
    - 低 tier(blog 等)を単独で「先行研究が否定」などの反証扱いにしない。

### 3.6 HARD GATE
- **責務:** 客観基準で自動 reject。現行 IDEA-stage の自動 reject は、必須項目欠落などの形不備に限定する。
  - 研究: 反証不能 / 完全な既出 / **保存則・proven定理に反する** などは verifier の確認対象。LLM の kill は自動 reject にしない。
  - 将来 PATCH-stage(future #21): tests fail / forbidden file / 無断 public API 変更 / secrets 検出 / 無断 network・install / diff 過大(→human) / security 弱化。
- **禁止:** **「通説・期待に反する」を kill しない。** それは高分散・高インパクトの種かもしれない → **残して flag**。kill するのは「証明に反する」場合のみ。
- **LLM の verdict='kill' は『推奨』であって客観 reject ではない(実装上の分離):** 自動 reject するのは**形不備など決定的・客観的な基準のみ**。LLM が kill と判断した候補は落とさず `kill?(LLM/要確認)` として残し、人間が棄却の妥当性を判断(誤kill救済)。
- **落選の扱い:** 消さずに **捨て案台帳** に保存(理由付き)。

### 3.7 ARBITER
- **責務:** gate 通過後に残った候補を**横断整理**する。idea段階では勝者や採用候補リストを選ばない。現行の `arbiter()` は LLM を呼ばない決定的 Python 処理。
  - 現行 IDEA-stage: `decision_matrix.*`、`priority.json`、`REPORT.md`、`candidate_reports.md` を作って人間に提示。
  - 将来 PATCH-stage(future #21): **rule-based**(hard gate + 重み付き soft score の決定的スコアラ)。合成したら **再 VERIFIER** にかける。
- **入力:** 残った候補 + `verdicts/*.json` + `evidence/*.json`。
- **出力:** `decision_matrix.*` + `priority.json` + 人間向け report。
- **禁止:**
  - **idea段階で勝者を選ばない**(客観指標が無い所で LLM に裁かせると §0 が壊れる)。
  - **soft score を単一値に潰さない**(matrix のまま人間へ)。
  - 妥当性を再判定しない(gate 通過は前提として扱う)。
- **priority_for_next_round(Issue #37):** matrix に併記される配分指針は **ranking ではなく allocation**
  — 「次ラウンドの**追加**検証予算をどこに使うか」を既存 artifact(軸 confidence / unknowns / flag /
  重複)から**決定的に**計算したもの(LLM 不使用)。採用/棄却には使わない。**floor 保証**: 低 priority でも
  棄却されず baseline 検証は全員が受け続ける(de-facto kill の禁止)。AI debate / pairwise を信号に使う場合も
  argument_trace(#36)として evidence と分離し、opt-in とする。
- **Research Priority Reviewer(#61/#59):** `research_priority.json` を作る optional LLM job。Arbiter の採用判断ではなく、研究として育てる順の LLM 推奨・要確認メモ。候補集合全体を見るため `stage_engine.research_priority` で指定でき、空文字なら secondary engine。

### 3.8 ARTIFACT BUS
- **責務:** 全コンポーネント間の受け渡しをファイルで行う(YAML/MD/JSON/diff)。
- **特性:** チャットなし。crash 耐性(ディスク永続)。デバッグ容易(ファイルを開けば履歴)。
- **provenance:** 全成果物に worker / model / seed / commit を刻印 → 再現可能・監査可能。

---

## 4. 検証コストラダー（低コスト確認 → 高コスト確認）

**まず少ない手間で確認できる検証を行い、費用・時間・人手がかかる検証は、そこを通った候補だけに使う。** これが効率の源泉。

| Tier | 何を見る | AI 単独でできること | 人間 |
|---|---|---|---|
| **0a 形** | 反証可能な仮説か(vs 漠然とした方向) | 主張・予測・棄却条件の有無を判定 | — |
| **0b 新規性** | 既出か / 最近接の先行研究 | **実DB検索**で prior art、差分の言語化 | — |
| **0c soundness** | 既知結果/保存則と矛盾しないか | 導出 sanity、矛盾の指摘 | 微妙な所の確認 |
| **0d feasibility** | 桁が合うか / 観測可能か | **toy計算・桁見積もり**(Codex) | 装置/予算の現実性 |
| **1 低コストな実確認** | 軽い計算 / **既存・公開データ再解析** / 同僚 gut-check | toy sim・再解析・分析 | 専門判断・解釈 |
| **2 費用・時間・人手がかかる本研究** | 実測定 / フルシミュ / フル解析 / 提案書 | 計装・コード | 実行・最終判断 |

判定: pass → 次の高コスト Tier、fail → 候補から外す or 問いを再定式化。

> idea段階の「検証」は正しさの **証明** ではなく **反証 + デリスク**。
> 出力は「この案は低コストの反証を通過した。残る最大リスクは X、証拠は Y」であって「validated」ではない。

**注意点:** AI に生成させた「ユーザー/専門家の反応」を pass の信号にしない(= AI判定への逆戻り)。合成反応は Tier 0 の事前フィルタまで。pass条件は必ず現実(実データ or 実際の人間)に置く。

---

## 5. 決定権の所在（設計の心臓）

| 層 | 担い手 | 役割 | 決定権 |
|---|---|---|---|
| 生成 | Claude / Codex / 戦略worker | 候補・仮説・patch を出す | **なし** |
| red-team | 別モデル | 反証仮説を**生む**(攻撃) | なし(検証へ回す) |
| 検証 | 文献DB / 計算 / soundness / test / CI | 客観的に**確認**し証拠を出す | 妥当性 gate のみ |
| 調停 | rule-based(patch) / 整理係(idea) | gate通過者を比較・絞る | 機械的絞り込みのみ |
| 最終 | **Human** | 採用 / merge / 実験予算配分 | **◎ 全部** |

一行で: **AIは候補を出す。裁くのは客観検証。決めるのは人間。**

---

## 6. 不変条件（常に成り立たせるルール）

実装がこれを破ったらバグ。レビューのチェックリストにもなる。

1. AI は採用判定を下さない(§5)。
2. 連携は成果物経由、チャット合議なし(§3.8)。
3. 生成は独立・blind(§3.3)。多様性を最優先。
4. **VERIFIER は生成者から独立**。gate は PLANNER/人間が事前固定(§3.5)。
5. **RED-TEAM は judge しない**。攻撃は `convert_to` で検証可能項目に変換(§3.4)。
6. HARD GATE: 証明違反 = kill、通説違反 = 残して flag(§3.6)。
7. soft score を単一値に潰さない。matrix で人間へ(§3.7)。
8. 捨て案・未解決論点を必ず残す(§3.6)。
9. 全成果物に provenance を刻む = 再現可能(§3.8)。
10. 検証は低コストな確認から始める。費用・時間・人手がかかる検証は、残った候補だけに使う(§4)。
11. 現行は1 round。複数 round 導入時は、round上限(3-5) + 実質差分ゼロで打ち切る(future #38)。

---

## 7. 成果物とスキーマ

### 7.0 主な artifact の役割

| artifact | 役割 | 正本 / view |
|---|---|---|
| `charter.json` | seed、制約、評価軸、発想レンズ(案を出すときの見方・切り口)を固定する | 正本 |
| `candidates/*.json` | 生成された候補仮説 | 正本 |
| `reviews/*.json` | red-team が出した攻撃・確認論点 | 正本 |
| `revised/*.json` | red-team 後の改訂版 | 正本 |
| `verdicts/*.json` | Tier0 検証結果 | 正本 |
| `evidence/*.json` | 文献・出典検索の生データ | 正本 |
| `proximity.json` | 近い候補・重複・未探索方向の注釈。棄却には使わない | 正本 |
| `hypothesis_graph.{json,md}` | seed→生成→攻撃→改訂→検証の lineage | 正本 / view |
| `REPORT.md` | 最初に読む run summary | view |
| `candidate_reports.md` | 候補ごとの人間向け詳細レポート | view |
| `decision_matrix.{json,md}` | 評価軸ごとの比較表。勝者は選ばない | 正本 / view |
| `priority.json` | 次に検証する順番。決定的計算 | 正本 |
| `research_priority.json` | 研究として育てる順の LLM 推奨。採用判定ではない | 正本 |
| `fallbacks.json` | LLM job の失敗・timeout・出力不備を別手段で補って完走したかの記録 | 正本 |
| `control/operator_notes.jsonl` | 実行中に人間が入れた steering note | 正本 |
| `memory_suggestions.{json,md}` | memory に記録する候補。自動保存はしない | 正本 / view |
| `discarded.md` | 形不備など客観的な不備で自動棄却された候補 | view |
| `unresolved.md` | 未解決論点・未追跡の stronger_variant | view |
| `status.json` / `events.jsonl` | watchdog / run 状態の記録 | 正本 |

Markdown は基本的に人間向け view であり、JSON artifact が正本。例外的に README / ARCHITECTURE / prompts / templates は設計・運用の正本として扱う。

### 7.1 Research Hypothesis Contract（idea段階の中核成果物）

```yaml
id: rq-001
question: "..."                       # 研究の問い
hypothesis: "..."                     # 反証可能な定量的主張/予測
novelty_claim: "最近接の先行研究は X。本案は Y の点で異なる"
soundness: "既知の[定理/保存則]に反しない。[確立理論]に立脚。鍵となる導出: ..."
falsification: "[測定/シミュ]が ... を示せば棄却"
test_method: "toy MC / データセットD の再解析 / 解析計算 / 提案測定"
feasibility: "必要統計/ルミ/感度/計算 = X。利用可能 = Y。見積り: ..."
significance_if_true: "..."           # 正しければ何が変わるか
risk_type: novelty | soundness | feasibility | significance
cheapest_kill: "最初に試す反証方法。少ない手間で否定できる単一チェック"  # 必須
assumptions: ["前提として明示すること"]       # 必須
unknowns: ["まだ分からないこと"]             # 必須
baseline: "比較すべき基準"                   # 任意
success_metric: "成功したと言える指標"        # 任意
failure_condition: "この条件なら候補を見直す"  # 任意
search_keywords: ["english keyword"]          # 任意。文献検索の recall 用
changes: ["revise で何を変えたか"]             # 任意。revise の自己申告
resolved_red_team_issues: ["潰した指摘"]       # 任意。revise の自己申告
provenance: {worker: ..., engine: ..., model: ..., seed: ..., commit: ...} # orchestrator が付与
```

### 7.2 Red-team review（attack の出力）

```yaml
# review of rq-001  (著者は伏せる / blind が望ましい)
attacks:
  - type: hidden_assumption
    claim: "前提[X]が必要だが未明示"
    convert_to: assumption          # → revised の assumptions に反映し、Tier0で確認
  - type: contradicting_work
    claim: "[paper Y]が類似測定で否定的?"
    convert_to: lit_check           # → VERIFIER が実DBで確認
    pointer: "arXiv:..."
  - type: feasibility_hole
    claim: "背景Zを無視。含めると桁が変わる"
    convert_to: computation         # → Codex が toy で再計算
  - type: confound
    claim: "提案測定はAとBを分離できない"
    convert_to: falsification_fix   # → test_method を改訂
  - type: stronger_variant
    claim: "対象をWにすると新規性↑"
    convert_to: new_candidate       # → unresolved.md に残し、次 run / future evolution へ
# 禁止: 良い/悪い・スコア・どちらが勝ち
# convert_to の無い攻撃は捨てる
```

### 7.3 段階別の成果物一覧

- **IDEA(現行):** `charter.json` / `candidates/*.json` / `reviews/*.json` / `revised/*.json` / `verdicts/*.json` / `evidence/*.json` / `proximity.json` / `hypothesis_graph.*` / `decision_matrix.*` / `priority.json` / `research_priority.json` / `REPORT.md` / `candidate_reports.md` / `fallbacks.json` / `discarded.md` / `unresolved.md`
- **SPEC(future #9):** `spec.md` / `acceptance.md` / `constraints.yaml` / `test_plan.md` / `non_goals.md`
- **PATCH(future #21):** `patch.diff` / `test_results.txt` / `review.md` / `risk_report.md` / `score.json` / `selected.patch`
- **常設:** 捨て案台帳 / 未解決論点 / memory に記録する候補

### 7.4 Patch段階の hard gate / soft score(future #21)

- **hard gate(自動reject):** tests fail / forbidden file touched / 無断 public API 変更 / secrets / 無断 network・install / diff過大→human / security弱化。
- **soft score:** `+` regression test 追加 / small diff / 既存パターン踏襲 / 明確な root cause / 簡単な rollback。`-` 新規依存 / 広範 refactor / 弱いカバレッジ / 性能リスク / 不明なユーザー影響。
- **anti-gaming:** worker に見えない **hidden/held-out テスト**、`patch前に失敗→後に成功` の確認、既存テスト改変ロック。

---

## 8. 安全境界（2プレーン）

危険なのは「ネットを読むこと」ではなく **「読む × 操作できる × secret がある」の掛け算**。
そこで **「知る(Read)」と「やる(Write/Execute)」を分離**し、Read は広く開け、Write/Execute は締める。

### Read プレーン（情報取得）— 広く開放
- 現行 provider は **arXiv / INSPIRE-HEP / NASA NTRS**。ADS / TechPort / Semantic Scholar / 一般ウェブ検索は future provider。
- ただし **仲介ツール越し**(GET 専用・全クエリをログ)。raw socket は渡さない。
- 取得結果は **スナップショット保存** → 再現性(ライブの web は変わる。artifact 化し、以降の推論はそれに対して回す)。
- 取得したウェブ内容は **"データ" であって "命令" ではない** として扱う(prompt injection 封じ)。injection の最大被害は「悪い候補が1個」→ 検証+人間で落ちる。
- 出典は **tier 付け**(§3.5): 権威DB > 査読 > preprint > web/blog。

### Write/Execute プレーン（結果を伴う操作）— default-deny
- 現行 IDEA-stage の engine は ADR-001 の ConPTY(pywinpty) 対話セッションで動かす。
- worker の書き込みは repo 内の queue / run artifact に限定する設計。
- raw network egress、rm、deploy、credential 操作は禁止。
- package install / 外部書込通信は **approval 必須**(検索 ≠ install ≠ raw network。混ぜない)。
- 将来 PATCH-stage(future #21)では、コード実行 sandbox、allowlist された test command、patch size / 変更ファイル数の上限を追加する。
- **merge は人間または明示的 Arbiter 承認後**(緑のCI ≠ 正しさ)。

### 共通
- 現行実装は worktree 隔離ではない。生成時に兄弟候補を prompt に渡さないことで独立性を確保する。
- **研究 worker に secret を置かない**(持ち出すものが無ければ exfiltration は成立しない)。
- enforcement は **sandbox/OS レベル**。プロンプトの「ネット使うな」は安全制御として信用しない(生成コード・injection はプロンプトでは止まらない)。

---

## 9. ガードする失敗モード

| 失敗モード | 対策 |
|---|---|
| 同調(sycophancy)・偽の合意 | AI に判定させない(§0)。red-team は judge 禁止(§3.4)。 |
| 自己採点バイアス | VERIFIER を生成者から独立(§3.5)。 |
| 高分散・高インパクト案を保守フィルタが棄却する | 証明違反のみ kill、通説違反は残して flag(§3.6)。gate をタスク種別で可変に。 |
| test-gaming(もっともらしいが誤った patch) | hidden テスト / fail→pass 確認 / テスト改変ロック(§7.4)。 |
| idea段階を並列トーナメント化して破綻 | ファネルとして作る。低コスト確認から高コスト確認へ進む(§2, §4)。 |
| 幻覚引用・低品質出典で誤った新規性判定 | 実DB検索 + 出典の tier 付け(§3.5)。 |
| red-team が案を均質化 | マージ圧をかけず各案を独立に鋭くする(§3.4)。 |
| 過剰設計(3段×多成果物を一気に作る) | MVP は Patch でなく **IDEA の1ループ**から(§11)。 |

---

## 10. 基盤マッピング（現行: ADR-001）

- 現行 worker = engine ごとの ConPTY(pywinpty) 対話セッション。
- 受け渡し = queue/report protocol。完了は report file の atomic rename で検知する。
- `codex exec` / `claude -p` は使わない。両方とも対話セッションとして駆動する。
- shogun 的 tmux/worktree backend は不採用(rejected)。理由と実機検証は `docs/adr-001-worker-driving.md` が正本。
- 将来 PATCH-stage で worktree / sandbox を使う場合は future #21 の範囲で再設計する。

---

## 11. 現行スコープ

**重心は IDEA なので、Patch から作らない。** 現行実装は、研究の種1件を次の1 roundで処理する:

1. 研究者が **seed(問い or hunch) + 制約(使える装置/データ/計算資源)** を入力。
2. 複数 worker が**発想レンズ(案を出すときの見方・切り口)**で候補仮説を N 個生成(独立)。
3. **PROXIMITY** が近い候補・重複・未探索方向を注釈する(棄却には使わない)。
4. **RED-TEAM** が攻撃 → 検証可能項目に変換。dual では生成 engine と別 engine に割り当てる。
5. 攻撃を受けて候補を **1回だけ revise** する(原案は保存)。
6. **VERIFIER** が Tier 0 を実行: 形式、文献候補、筋の良さ、実行しやすさを確認。dual では生成 engine と別 engine に割り当てる。
7. **HARD GATE** は形不備など客観的な不備だけを自動棄却する。LLM の kill は `kill?(LLM/要確認)` として残す。
8. **ARBITER** が `decision_matrix.*`, `priority.json`, `REPORT.md`, `candidate_reports.md` に整理する。必要なら optional LLM の Research Priority Reviewer が `research_priority.json` を作る。
9. 研究者が次に検証する候補に**探索予算を配分**する。

**モード:** コールド生成より、まず **「seed あり de-risk・拡張」モード** を作る(信頼でき、すぐ実研究に使える)。

provenance には `session_scope: "shared_engine_session"` を記録する。これは「同一 engine 内では常駐セッションを共有する」ことを意味する。

---

## 12. 未決事項（要決定 / 要検証）

- **[要検証] H2:** 「並列生成は単一agentより良い候補を増やす」は未検証。**この設計全体がここに乗っている。** MVP 自体を H2/H3/H4 の実験にする。
- **[要検証] ベンダー多様性 vs 戦略多様性:** 「単一モデル+複数戦略」と「Claude+Codex 混在」を同じ VERIFIER で比較。混在の追加価値を測る。
- **[一例実装済み / 要拡張] 分野の寄せ方:** `configs/spacecraft.json` は実装済み。宇宙機では NASA NTRS primary / INSPIRE trigger。未決なのは他分野の domain config。
- **[要決定] SPEC 段階に専用エンジンは要るか:** 現状「Planner が spec.md → 人間承認」で足りる見込み(トーナメント不要)。
- **[要決定] cold-generation を入れるか:** 当面は seeded-derisk のみで開始。

---

## 13. 用語集

| 用語 | 意味 |
|---|---|
| artifact | LLM や検証器が残すファイル成果物。判断はチャットではなく artifact を見る。 |
| charter | run の目的、制約、評価軸、発想レンズを固定したもの。以後の工程は charter を前提に動く。 |
| lens / 発想レンズ | 候補を出すときの見方・切り口。engine ではない。同じ問いを別の観点から眺めるために使う。 |
| proximity | 似た候補や重複を整理する層。注釈であり、棄却には使わない。 |
| red-team | 候補の弱点、隠れた前提、反証、交絡を出す工程。judge ではない。 |
| verifier | 文献、形式、筋の良さ、実行しやすさなどを確認する工程。候補同士は比較しない。 |
| hard gate | 客観的にダメなものだけ落とす工程。LLM の kill は推奨扱いであり、自動棄却ではない。 |
| arbiter | 勝者を決めず、人間が判断しやすい形に整理する工程。 |
| evidence | 文献、計算、実験、CI 結果などの検証材料。AI の議論や感想とは分ける。 |
| argument_trace | AI の指摘、議論、反論の記録。注意を動かせるが、evidence level は上げない。 |
| priority_for_next_round | 次に検証する順番。決定的計算で作る。採用判定ではない。 |
| research_priority | 研究として育てる順の LLM 推奨。要確認であり、採用判定ではない。 |
| fallback | LLM job の失敗、timeout、出力不備を別 engine や既定値で補って run を止めずに続けた記録。`count > 0` の run は通常より注意して読む。 |
| operator steering | 実行中に人間が注意点を artifact として追記する仕組み。evidence ではない。 |
| session_scope | LLM セッション履歴の共有範囲。現行は `shared_engine_session` で、同一 engine 内では session-level の履歴を共有する。 |
| 新しさ(novelty) | 既知研究との差。README や report ではこの日本語ラベルを使う。 |
| 筋の良さ(soundness) | 物理・理屈として破綻していないか。 |
| 実行しやすさ(feasibility) | 最初の検証に必要なデータ、計算、工数が現実的か。 |
| impact / インパクト(significance) | 正しければどの程度役に立つか。 |
| 最初に試す反証方法(cheapest_kill) | その候補を少ない手間で否定できるかを最初に見る確認。 |

---

*このドキュメントは設計の単一ソース。実装がここを破ったら、実装かこの文書のどちらかを直す。*
