# ARCHITECTURE — Claude/Codex 候補生成 + 客観検証 + 人間レビュー システム

研究の初期アイディアだしを主目的に、開発支援(実装)まで同じ骨格でつなぐマルチエージェント設計。
**Claude/Codex のペアプロでも、AI同士の合議でもない。** AI は候補を出すだけ。裁くのは客観検証、決めるのは人間。

---

## 0. 設計思想（最初に固定する原則）

1. **AI同士に「どちらが正しいか」を聞かない。** AI は候補・仮説・patch・test・review・risk を出す。正しさ/採用可否は、test / lint / typecheck / CI / 文献 / 計算 / 実験 / 人間レビューが判定する。
2. **連携は成果物(artifact)経由。自然言語チャットで合議しない。**
3. **AI の合意を merge 条件にしない。** 合意は意味のある信号ではない。
4. **最終判断は Human が持つ。**
5. **重心は「実装」より「研究の初期段階」。** 曖昧なアイディアを「検証可能な仮説」と「最小実験」に変換することを最重要視する。

> この5つは load-bearing。以降の設計は全部この帰結。迷ったら 0 に戻る。

---

## 1. 全体フロー

```
                         ┌─────────┐
                         │  HUMAN  │  種/問い/タスク + 制約
                         └────┬────┘
                              ▼
                     ┌─────────────────┐
                     │     PLANNER      │  charter を固定(以後ロック)
                     │ 目的分解・成功条件 │  hard gate を定義
                     │ 範囲・禁止操作・評価軸│ ※gateは生成者から独立
                     └────────┬────────┘
                              │ charter.yaml
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼      並列・隔離
   ┌───────────┐       ┌───────────┐       ┌───────────┐  (worktree)
   │  Claude   │       │   Codex   │       │  別戦略    │  互いを見ず
   │ 概念/枠組  │       │ 計算/実装  │       │ アナロジー │  独立生成
   │ 文献/soundness│   │ sim/feasibility│  │ contrarian │  (多様性が命)
   └─────┬─────┘       └─────┬─────┘       └─────┬─────┘
         └────────────────────┼────────────────────┘
                              ▼  候補(idea/hypothesis/patch)
                     ┌─────────────────┐
              ┌─────▶│     RED-TEAM     │  相手案を"攻撃"して鋭くする
              │      │  (cross-review)  │  隠れ前提/交絡/反例/feasibility穴
              │      │  ※judgeしない     │  → 攻撃を"検証可能項目"に変換
              │      └────────┬────────┘  → 強い変種は新候補に
              │               ▼
              │      ┌─────────────────┐
              │ 研究→ │     VERIFIER     │  候補ごと・客観・他候補を見ない
              │ 実装→ │ 文献(実DB)/計算/  │  生成者から独立
              │      │ soundness ‖      │
              │      │ test/lint/CI/diff │
              │      └────────┬────────┘  evidence + score.json
              │               ▼
              │      ╔═════════════════╗
              │      ║    HARD GATE     ║  客観で自動 reject
              │      ║ 反証不能/既出/    ║  保存則違反=kill
              │      ║ test fail/危険操作 ║  通説違反=残してflag
              │      ╚════════┬════════╝  落選→捨て案台帳(消さない)
              │               ▼  通過候補のみ
              │      ┌─────────────────┐
              │      │     ARBITER      │  候補横断・比較
              └──────┤ patch: rule-based │  idea: 整理係(勝者を選ばない)
            fail→再定式 │ 合成したら→再Verify │  → decision_matrix + shortlist
            (3-5round上限)└────────┬────────┘
                              ▼
                         ┌─────────┐
                         │  HUMAN  │  採用/merge/方向転換
                         └─────────┘  /実験予算の配分  ◎最終決定

  ═══ 全ての受け渡しは Artifact Bus(ファイル)経由。自然言語チャットなし ═══
       全成果物に provenance(worker/model/seed/commit)を刻印 = 再現可能
```

---

## 2. 3段ファネル（同じ骨格が段階で特殊化する）

コミットが深まるほど、**検証者の正体が「人間判断寄り」→「完全客観」へ移る。** だから単一エンジンでは作らない。

| | STAGE 1 **IDEA**(研究の種) | STAGE 2 **SPEC** | STAGE 3 **PATCH** |
|---|---|---|---|
| 目的 | 検証可能な仮説に加工する | MVP範囲/受入条件を確定 | 動く実装を選ぶ |
| 検証者 | **安い→高いラダー**(§4) | acceptance の検証可能性(半客観) | test/lint/CI/diff(客観・安い・並列) |
| 性質 | 人間中心ファネル / 候補少・検証高い | 軽量(Planner+人間承認で足りる) | 本物の並列トーナメント |
| AI の効き所 | **ここに重心** | 補助 | 候補を投げ放題、機械で殺せる |
| トーナメント成立 | ❌(ファネルとして作る) | △ | ✅ |

**注意:** STAGE 1 を STAGE 3 と同じ「並列トーナメント」として実装してはいけない。検証コストの非対称性が逆転している(idea は生成が安く検証が高い)。

---

## 3. コンポーネント責務

各コンポーネントは「責務 / 入力 / 出力 / 禁止事項」で定義する。**禁止事項が設計の本体**(原則 §0 を局所化したもの)。

### 3.1 HUMAN
- **責務:** 種/問い/タスクと制約の投入。最終採用・merge・方向転換。**idea段階では「どの生存案に実験予算を割くか」の配分。**
- **決定権:** ◎ 全部。

### 3.2 PLANNER
- **責務:** 目的分解、成功条件、触ってよい範囲、禁止操作、評価軸、**hard gate の定義**を `charter.yaml` に固定(以後ロック)。
- **入力:** Human の種+制約。
- **出力:** `charter.yaml`。
- **禁止:** 自分で案を出さない(隠れ worker 化の禁止)。gate を曖昧にしない。

### 3.3 WORKERS（並列・隔離・独立）
- **責務:** charter を受け、**互いを見ずに** 候補を生成。
  - **Claude軸:** 概念/枠組み、文献統合、soundness 批判、proposal 文章化、UX/価値の言語化。
  - **Codex軸:** 桁見積もり、toy simulation、データ再解析スクリプト、feasibility の数値検証、patch/test。
  - **別戦略軸:** アナロジー/転用、anomaly駆動、method駆動、組合せ、contrarian、gap駆動 — レンズを変えて多様性を稼ぐ。
- **入力:** `charter.yaml`。
- **出力:** 候補成果物(idea_card / Research Hypothesis Contract / patch.diff 等)。
- **隔離:** 各 worker は別 worktree / sandbox。
- **禁止:** 生成段階で他 worker の案を参照しない(独立性=多様性の源)。

> ベンダー多様性(Claude vs Codex)より、**戦略多様性(レンズの違い)**の方が候補の質に効く。
> 「単一モデル+複数戦略プロンプト」でも多様性は出る。ベンダー混在の追加価値は §11 の H2 で検証する。

### 3.4 RED-TEAM（cross-review を付け替えたもの）
- **責務:** 他 worker の案を **攻撃して鋭くする**。隠れ前提、交絡、反例、feasibility 穴、強い隣接変種を出す。
- **入力:** 候補成果物(著者は伏せる=blind が望ましい)。
- **出力:** `attacks[]`。各攻撃は **`convert_to`** で検証可能項目に変換される(§7.2)。変換できない攻撃は捨てる。
- **禁止:**
  - **judge しない。** 「良い/悪い」「どちらが勝ち」「スコア」を出さない(これは VERIFIER と HUMAN の領分)。
  - **均質化させない。** 案をマージ/合意へ寄せる圧をかけない。攻撃されたら原案を改訂(justified diff)するか、原案と改訂版を2候補として残す。
- **位置づけ:** critic は「反証仮説を生成」するだけ。正しいかは VERIFIER が客観確認する(critique 自体に generate-and-verify を再帰)。critic は VERIFIER の recall を上げる役で、裁判官ではない。

### 3.5 VERIFIER
- **責務:** **候補ごとに・客観的に** 検証し、証拠(evidence)とスコアを出す。
  - 研究段階: 文献(実DB検索) / 計算・toy sim / soundness(理論整合)。
  - 実装段階: test / lint / typecheck / CI / diff-guard。
- **入力:** 候補 + red-team が出した検証可能項目。
- **出力:** evidence(再現可能な根拠) + `score.json`。
- **禁止:**
  - **候補同士を比較しない**(横断は ARBITER の仕事)。
  - **「良し悪し」を主観判定しない。** 測れる事実だけを出す。
  - **生成者と同一であってはならない**(Codex worker が自分用の Verifier/gate を設計するのは利益相反。gate は PLANNER/人間が事前固定)。
- **重要(研究の新規性):** 文献チェックは AI の記憶でやらない。**実DB(INSPIRE-HEP / arXiv / Semantic Scholar / ADS) + 一般ウェブ検索** で検索する(幻覚引用・recall漏れを防ぐ)。一般ウェブは breadth/recall 用。**出典を tier 付けして evidence に記録**する:
    - 権威DB(INSPIRE/arXiv) > 査読論文 > preprint > web/blog
    - 低 tier(blog 等)を単独で「先行研究が否定」などの反証扱いにしない。

### 3.6 HARD GATE
- **責務:** 客観基準で自動 reject。
  - 研究: 反証不能 / 完全な既出 / **保存則・proven定理に反する** / どんな現実的アクセスでも資源が届かない。
  - 実装: tests fail / forbidden file / 無断 public API 変更 / secrets 検出 / 無断 network・install / diff 過大(→human) / security 弱化。
- **禁止:** **「通説・期待に反する」を kill しない。** それは高分散・高インパクトの種かもしれない → **残して flag**。kill するのは「証明に反する」場合のみ。
- **落選の扱い:** 消さずに **捨て案台帳** に保存(理由付き)。

### 3.7 ARBITER
- **責務:** gate 通過候補を**横断比較**し、絞る。合成したら **再 VERIFIER** にかける。
  - 実装段階: **rule-based**(hard gate + 重み付き soft score の決定的スコアラ)。再現性が出る。
  - idea/spec段階: **整理係**。`decision_matrix` を作って人間に提示。
- **入力:** gate 通過候補 + evidence + score。
- **出力:** `decision_matrix` + shortlist(2-3候補)。
- **禁止:**
  - **idea段階で勝者を選ばない**(客観指標が無い所で LLM に裁かせると §0 が壊れる)。
  - **soft score を単一値に潰さない**(matrix のまま人間へ)。
  - 妥当性を再判定しない(gate 通過は前提として扱う)。

### 3.8 ARTIFACT BUS
- **責務:** 全コンポーネント間の受け渡しをファイルで行う(YAML/MD/JSON/diff)。
- **特性:** チャットなし。crash 耐性(ディスク永続)。デバッグ容易(ファイルを開けば履歴)。
- **provenance:** 全成果物に worker / model / seed / commit を刻印 → 再現可能・監査可能。

---

## 4. 検証コストラダー（研究 idea 段階）

**安い検証で先に殺し、高い検証(人間・実験)は生存者だけに使う。** これが効率の源泉。

| Tier | 何を見る | AI 単独でできること | 人間 |
|---|---|---|---|
| **0a 形** | 反証可能な仮説か(vs 漠然とした方向) | 主張・予測・kill条件の有無を判定 | — |
| **0b 新規性** | 既出か / 最近接の先行研究 | **実DB検索**で prior art、差分の言語化 | — |
| **0c soundness** | 既知結果/保存則と矛盾しないか | 導出 sanity、矛盾の指摘 | 微妙な所の確認 |
| **0d feasibility** | 桁が合うか / 観測可能か | **toy計算・桁見積もり**(Codex) | 装置/予算の現実性 |
| **1 安い実test** | 軽い計算 / **既存・公開データ再解析** / 同僚 gut-check | toy sim・再解析・分析 | 専門判断・解釈 |
| **2 本研究** | 実測定 / フルシミュ / フル解析 / 提案書 | 計装・コード | 実行・最終判断 |

判定: pass → 次の(高い)Tier、fail → 種の破棄 or 問いの再定式化。

> idea段階の「検証」は正しさの **証明** ではなく **反証 + デリスク**。
> 出力は「この案は安い反証を生き延びた。残る最大リスクは X、証拠は Y」であって「validated」ではない。

**罠:** AI に生成させた「ユーザー/専門家の反応」を pass の信号にしない(= AI判定への逆戻り)。合成反応は Tier 0 の事前フィルタまで。pass条件は必ず現実(実データ or 実際の人間)に置く。

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
10. 検証は安い→高いの順。高い検証は生存者だけ(§4)。
11. 収束は round上限(3-5) + 実質差分ゼロ で打ち切る。

---

## 7. 成果物とスキーマ

### 7.1 Research Hypothesis Contract（idea段階の中核成果物）

```yaml
id: rq-001
question: "..."                       # 研究の問い
hypothesis: "..."                     # 反証可能な定量的主張/予測
novelty_claim: "最近接の先行研究は X。本案は Y の点で異なる"
prior_art: [<INSPIRE/arXiv の実引用>] # AIの記憶ではなく実検索結果
soundness: "既知の[定理/保存則]に反しない。[確立理論]に立脚。鍵となる導出: ..."
falsification: "[測定/シミュ]が ... を示せば棄却"
test_method: "toy MC / データセットD の再解析 / 解析計算 / 提案測定"
feasibility: "必要統計/ルミ/感度/計算 = X。利用可能 = Y。見積り: ..."
significance_if_true: "..."           # 正しければ何が変わるか
risk_type: novelty | soundness | feasibility | significance
cheapest_kill: "この案を最も安く反証できる単一チェック"  # 必須
evidence: [calc.ipynb, lit_search.md, ...]
next_if_survives: "..."
next_if_killed: "問い or 価値仮説の見直し"
provenance: {worker: ..., model: ..., seed: ..., commit: ...}
```

### 7.2 Red-team review（attack の出力）

```yaml
# review of rq-001  (著者は伏せる / blind が望ましい)
attacks:
  - type: hidden_assumption
    claim: "前提[X]が必要だが未明示"
    convert_to: assumption          # → assumptions.yaml、Tier0で検証
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
    convert_to: new_candidate       # → 新 idea_card
# 禁止: 良い/悪い・スコア・どちらが勝ち
# convert_to の無い攻撃は捨てる
```

### 7.3 段階別の成果物一覧

- **IDEA:** Research Hypothesis Contract / assumptions.yaml / unknowns.md / validation_plan.md / risk_report.md / decision_matrix.json
- **SPEC:** spec.md / acceptance.md / constraints.yaml / test_plan.md / non_goals.md
- **PATCH:** patch.diff / test_results.txt / review.md / risk_report.md / score.json / selected.patch
- **常設:** 捨て案台帳 / 未解決論点

### 7.4 Patch段階の hard gate / soft score

- **hard gate(自動reject):** tests fail / forbidden file touched / 無断 public API 変更 / secrets / 無断 network・install / diff過大→human / security弱化。
- **soft score:** `+` regression test 追加 / small diff / 既存パターン踏襲 / 明確な root cause / 簡単な rollback。`-` 新規依存 / 広範 refactor / 弱いカバレッジ / 性能リスク / 不明なユーザー影響。
- **anti-gaming:** worker に見えない **hidden/held-out テスト**、`patch前に失敗→後に成功` の確認、既存テスト改変ロック。

---

## 8. 安全境界（2プレーン）

危険なのは「ネットを読むこと」ではなく **「読む × 操作できる × secret がある」の掛け算**。
そこで **「知る(Read)」と「やる(Write/Execute)」を分離**し、Read は広く開け、Write/Execute は締める。

### Read プレーン（情報取得）— 広く開放
- **一般ウェブ検索 + fetch を研究 worker に開放**(idea段の中核機能)。
- ただし **仲介ツール越し**(GET 専用・全クエリをログ)。raw socket は渡さない。
- 取得結果は **スナップショット保存** → 再現性(ライブの web は変わる。artifact 化し、以降の推論はそれに対して回す)。
- 取得したウェブ内容は **"データ" であって "命令" ではない** として扱う(prompt injection 封じ)。injection の最大被害は「悪い候補が1個」→ 検証+人間で落ちる。
- 出典は **tier 付け**(§3.5): 権威DB > 査読 > preprint > web/blog。

### Write/Execute プレーン（結果を伴う操作）— default-deny
- コード実行 sandbox は **完全 offline**(生成コード/テストを走らせるプロセス)。
- raw network egress は default-deny。
- 書き込みは workspace 内に限定。rm / deploy / credential 操作は禁止。
- package install / 外部書込通信は **approval 必須**(検索 ≠ install ≠ raw network。混ぜない)。
- allowlist された test command だけ自動実行(test 実行自体も sandbox 内)。
- patch size / 変更ファイル数に上限。
- **merge は人間または明示的 Arbiter 承認後**(緑のCI ≠ 正しさ)。

### 共通
- 各 worker は別 worktree / sandbox。
- **研究 worker に secret を置かない**(持ち出すものが無ければ exfiltration は成立しない)。
- enforcement は **sandbox/OS レベル**。プロンプトの「ネット使うな」は安全制御として信用しない(生成コード・injection はプロンプトでは止まらない)。

---

## 9. ガードする失敗モード

| 失敗モード | 対策 |
|---|---|
| 同調(sycophancy)・偽の合意 | AI に判定させない(§0)。red-team は judge 禁止(§3.4)。 |
| 自己採点バイアス | VERIFIER を生成者から独立(§3.5)。 |
| 高分散・高インパクト案を保守フィルタが殺す | 証明違反のみ kill、通説違反は残して flag(§3.6)。gate をタスク種別で可変に。 |
| test-gaming(もっともらしいが誤った patch) | hidden テスト / fail→pass 確認 / テスト改変ロック(§7.4)。 |
| idea段階を並列トーナメント化して破綻 | ファネルとして作る。安い→高い検証(§2, §4)。 |
| 幻覚引用・低品質出典で誤った新規性判定 | 実DB検索 + 出典の tier 付け(§3.5)。 |
| red-team が案を均質化 | マージ圧をかけず各案を独立に鋭くする(§3.4)。 |
| 過剰設計(3段×多成果物を一気に作る) | MVP は Patch でなく **IDEA の1ループ**から(§11)。 |

---

## 10. 基盤マッピング（任意: shogun 的 tmux+YAML+worktree に載せる場合）

- worker = worktree / tmux pane。
- 受け渡し = ファイル mailbox(YAML)。flock + inotify で zero-polling。
- VERIFIER = 非LLM の実行コンポーネント(CIランナー / 検索 / 計算)。
- 研究の文献検索 = INSPIRE-HEP / arXiv 連携。
- 多CLI割当 = `config/settings.yaml` で role ごとに type/model。
- instructions は単一ソースから各 role 向けに生成。

---

## 11. MVP スコープ（最初に作るもの）

**重心は IDEA なので、Patch から作らない。** 全体図の左下ループ1周を、研究の種1件で回す:

1. 研究者が **seed(問い or hunch) + 制約(使える装置/データ/計算資源)** を入力。
2. 複数 worker が**発散レンズ**で候補仮説を N 個生成(独立)。
3. **RED-TEAM** が攻撃 → 検証可能項目に変換。
4. 攻撃を受けて候補を **1回だけ revise** する(原案は保存)。
5. **VERIFIER** が Tier 0 を実行: 検索で新規性(実DB) / Codex で桁見積もり・toy / Claude で soundness。
6. Research Hypothesis Contract に成果物化、hard gate で死んだ線を落とす。
7. **decision_matrix**(新規性・feasibility・cheapest_kill 付き)で研究者に提示。
8. 研究者が深掘り対象に**探索予算を配分**。

**モード:** コールド生成より、まず **「seed あり de-risk・拡張」モード** を作る(信頼でき、すぐ実研究に使える)。

---

## 12. 未決事項（要決定 / 要検証）

- **[要検証] H2:** 「並列生成は単一agentより良い候補を増やす」は未検証。**この設計全体がここに乗っている。** MVP 自体を H2/H3/H4 の実験にする。
- **[要検証] ベンダー多様性 vs 戦略多様性:** 「単一モデル+複数戦略」と「Claude+Codex 混在」を同じ VERIFIER で比較。混在の追加価値を測る。
- **[要決定] 分野の寄せ方:** 物理のどの領域(実験/解析/理論/加速器/計算)か。文献DB(INSPIRE中心か)・発散レンズ・feasibility 計算系のテンプレが変わる。KEK なら INSPIRE-HEP 連携はほぼ確定。
- **[要決定] SPEC 段階に専用エンジンは要るか:** 現状「Planner が spec.md → 人間承認」で足りる見込み(トーナメント不要)。
- **[要決定] cold-generation を入れるか:** 当面は seeded-derisk のみで開始。

---

*このドキュメントは設計の単一ソース。実装がここを破ったら、実装かこの文書のどちらかを直す。*
