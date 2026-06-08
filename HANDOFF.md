# HANDOFF — claude-codex-orchestrator(作業継続メモ)

最終更新: 2026-06-09 / リポジトリ: https://github.com/hide709/claude-codex-orchestrator(private)
設計の正本: **[ARCHITECTURE.md](./ARCHITECTURE.md)**(思想§0・不変条件§6)/ 振る舞いの正本: **[AGENTS.md](./AGENTS.md)**

> このファイルは「次に開いた人(将来の自分 / 別セッションの AI)が即座に文脈を取り戻して続けられる」ためのもの。
> 迷ったら ARCHITECTURE.md §0 と本ファイルの「4. 大切なこと」に戻る。

---

## 0. いま何者か / 即・再開するには

**何を作っているか**: 研究の初期アイディアを「検証可能な仮説」と「最小実験」に変換する **IDEA-stage funnel**。
AI(Codex / Claude)は**候補を出すだけ**。裁くのは**客観検証**(形 / 文献 / 計算 / soundness)、決めるのは**人間**。
将来は `program_creater`(Workspace OS)へマージする前提(AGENTS.md「マージ地図」)。

**現在の状態**: `main` に PR #1/#2/#22/#26/#28 まで全部入り。**オープン PR は無い**。
パイプライン = `PLANNER → GENERATE(独立・並列) → RED-TEAM → REVISE(1回) → VERIFY(Tier0) → HARD GATE → ARBITER`。

**即・再開コマンド(Windows / PowerShell)**:
```powershell
cd C:\Users\hide\Documents\work\orchestration
git checkout main && git pull

# (A) 仕組みだけ確認(トークン/常駐セッション不要・オフライン)
python orchestrate.py --engine mock --no-lit-search --seed "テストの問い"

# (B) 実機 dual = Codex + Claude。タブを分けて常駐 worker を立ててから回す
#   タブ1:  .\tools\start-worker.ps1 codex
#   タブ2:  .\tools\start-worker.ps1 claude
#   タブ3:  python orchestrate.py --seed "あなたの研究の問い" --constraints "使える装置/データ/計算"
```
出力は `runs/<id>/`。まず `REPORT.md` と `decision_matrix.md` を読む。
良い候補は `python orchestrate.py promote <run> <id> --note "理由"`、ダメな線は `reject ...`、方針は `prefer "..."` で memory に記録 → 次回生成に反映。

**主要ファイル**: `orchestrate.py`(オーケストレータ本体・LLMではない) / `prompts/{ideator,redteam,verifier,revise}.md` / `worker/INSTRUCTIONS.md`(常駐 worker 手順) / `tools/start-worker.ps1`・`tools/mock_worker.py` / `memory/`(cross-run) / `config.json`。

---

## 1. 今までやったこと

設計対話 → MVP → レビュー往復 → 機能拡張、という流れで進んだ。

- **設計を固めた**: 「AI同士で正しさを議論させる」案を捨て、**generate-and-verify**(並列候補生成 → 客観検証 → 良い成果物だけ採用)へ転換。`ARCHITECTURE.md`(全体図 / 3段ファネル / コンポーネント責務 / 検証コストラダー / 不変条件 / 安全境界 / 未決事項)を単一ソースとして作成。
- **MVP を実装**(`orchestrate.py`、標準ライブラリのみ): IDEA funnel 1周。codex 実機で muon g-2 の妥当な仮説生成 + verifier の実文献ベース反論を確認。
- **git / GitHub 整備**: private repo 作成、`todo`/`future`/`merge-prep` ラベル、機能・将来構想・非機能を issue 化(#3–#21, #24, #25, #27)。
- **PR レビューの往復**(= システムの思想を地で実践):
  - **#1**(マージ済): revise stage、arXiv 文献収集、provenance、codex 解決の堅牢化(Codex 製を私がレビュー→マージ)。
  - **#2**(マージ済): **客観 hard gate と LLM 判断(kill→flag)の分離**、**INSPIRE-HEP collector**、arXiv の source_tier を preprint に修正。
  - **#22**(マージ済): `AGENTS.md`(統治 + program_creater への「マージ地図」)。
  - **#26**(マージ済): **cross-run memory**(promote/reject/prefer、重複検知、生成・検証へ反映)。
  - **#28**(マージ済): **デュアルエンジン完全統一** — codex も claude も headless(codex exec / claude -p)を廃止し、両方「常駐セッション + ファイル queue」に統一。全ステージ queue 経由、engine 別 heartbeat、生存判定、`tools/start-worker.ps1 <engine>`、`mock_worker.py`、原子的ファイル書き込み。
- **環境の壁を解決**(下記「3. 気づき」のgotchas)。

---

## 2. これからやること(open issues)

ラベルで2層: `todo`(近め) / `future`(構想) / `merge-prep`(program_creater 統合準備)。

**推奨の着手順(私見・更新前提)**:
1. **実機 dual を1本回す**(issue ではない運用): codex+claude の常駐 worker を立てて実研究の種で回し、両エンジンの候補と verifier の挙動を体感。memory に promote/reject を貯め始める。
2. **#16 tests + CI**(mock smoke + 不変条件)→ 土台。これがあると以降の改造が安全。
3. **#14 two-plane 安全境界**(egress allowlist / untrusted evidence / secrets)→ #17/#21 の前提。
4. **#17 computational verification**(verifier が sandbox で toy 計算 → feasibility を散文でなく数値 evidence に)→ 研究 ideation の信頼性が一段上がる。**いま一番効く**。
5. **#12 H2 ベンチ**(並列 vs 単一、混在の価値を測る)→ 設計の根仮説の検証。
6. **#24 dashboard / #25 selection 語彙**(merge-prep)→ program_creater へ畳みやすくする。

**残り(随時)**: #3 role別worker / #4 Semantic Scholar・ADS / #5 arXivクエリ改善 / #6 stronger_variant 再投入 / #8 multi-round 収束 / #9 Spec・Prototype stage / #10 CI・PR連携 / #11 shogun/tmux backend / #13 human 決定 UI / #15 verifier 独立性 / #18 Tier1 実験雛形 / #19 一般web検索 / #20 cold生成 / #21 Patch Tournament engine。

---

## 3. 今までの中での気づき

### 設計の気づき(なぜ今の形か)
- **AIに「正しさ」を裁かせない。** 候補生成と客観検証を分離するだけで、同調(sycophancy)・自己採点バイアス・偽の合意がまとめて消える。これが全設計の根。
- **検証コストの非対称性は段階で逆転する。** Patch は検証が安く客観(=並列トーナメント成立)。Idea は検証が高く人間依存(=トーナメントでなく「安い→高い」ファネル)。**研究アイデアはプロダクト案より机上検証しやすい**(新規性=文献 / soundness=理論 / feasibility=桁計算が desk-checkable)ので generate-and-verify と相性が良い。
- **idea段の「検証」は証明でなく反証+デリスク。** 出力は「validated」ではなく「安い反証を生き延びた / 最大リスクは X」。
- **cross-review は judge させず「攻撃→検証可能項目に変換」させる。** critic は反証仮説を生むだけ、正否は verifier が確認(= generate-and-verify の再帰)。
- **客観 gate と LLM 判断の分離。** 自動 reject は形不備など客観のみ。LLM の kill は `kill?(LLM/要確認)` として残し人間が判断(誤kill救済)。
- **戦略多様性 > ベンダー多様性。** レンズ(発想角度)の違いの方が候補の質に効く。Claude+Codex 併用の追加価値(H2)は未検証 → #12。
- **2プレーン安全境界。** 「読む(検索)」は広く開け、「やる(実行・書込)」は締める。危険は「読む × 操作できる × secret」の掛け算。
- **記憶は人間が採用/却下した知識だけ永続。** `runs/` は使い捨て staging、`memory/` が長期知識(program_creater と同型)。
- **常駐セッション + queue に統一(headless 廃止)。** 理由: `claude -p` は別料金 + program_creater/Shogun の「常駐セッション同士」方針に完全準拠。orchestrator は engine を直接呼ばず queue で疎結合。

### 実務の気づき(gotchas・ハマりどころ)
- **codex**: Windows の PATH に無く実体は `…\AppData\Local\OpenAI\Codex\bin\<hash>\codex.exe`。`~/.codex/config.toml` の `service_tier="default"` が CLI を壊す → `-c service_tier=flex` で回避(codex worker 起動スクリプトが使用)。
- **claude**: PATH を通しても**既存ターミナルは古い PATH を握っている** → 新しいターミナル(or レジストリから PATH 再読込)で見える。実体 `…\SPB_Data\.local\bin\claude.exe`。
- **対話起動**: `codex "<prompt>"` / `claude "<prompt>"`(`-p`/`exec` なし)= 課金されない対話セッション。worker は `codex -s workspace-write -a never` / `claude --permission-mode acceptEdits` で無確認書込。
- **Windows コンソール cp932**: Python stdout を utf-8 に reconfigure しないと絵文字/em-dash で落ちる(対応済み)。
- **構造化出力**: codex `--output-schema` は strict(全 property required + `additionalProperties:false`)。N/A は空文字/空配列で返させる。
- **revise が印を落とす**: revise は新 dict を作るので `_engine` / `_near_dup` 等の内部印を明示的に引き継ぐ必要があった(対応済み)。
- **preferences.md パース**: 箇条書き行のみ拾う(説明行を好みと誤読したバグを修正済み)。
- **queue は原子的に**: `.tmp` に書いて rename(途中読み防止)。worker 応答前に heartbeat 生存チェック(無限待ち回避)。
- **mock engine は命綱**: トークン/常駐なしで全配管をオフライン検証できる(`--engine mock`、queue は `tools/mock_worker.py <engine>`)。

---

## 4. 大切なこと(壊してはいけない原則)

**ARCHITECTURE.md §0(load-bearing な5原則)**:
1. AI同士に「どちらが正しいか」を聞かない。正しさ/採用可否は test/lint/CI/文献/計算/実験/人間が判定。
2. 連携は**成果物(artifact)経由**。チャットで合議しない。
3. AI の合意を merge 条件にしない。
4. 最終判断は Human。
5. 重心は「実装」より**研究の初期段階**。

**実装の不変条件(ARCHITECTURE §6 / 破ったらバグ)**:
- orchestrator(`orchestrate.py`)は **LLM ではない**(ループ・型強制・gate・集計のみ)。
- VERIFIER は生成者から独立 / RED-TEAM は judge しない(攻撃→convert)。
- 自動 reject は**客観基準のみ**。LLM kill は落とさず人間へ。
- soft score を単一値に潰さない(matrix で人間へ)。捨て案・未解決論点は消さない。
- 全成果物に provenance(worker/model/seed/commit)。再現可能。
- 検証は安い→高いの順。高い検証(人間・実験)は生存者だけ。
- **永続するのは人間が記録した知識だけ。AI 判断で自動棄却しない。**

**進め方のメタ原則(この往復で効いたこと)**:
- 変更は**小さく → PR → レビュー → マージ**。設計と実装が乖離したら ARCHITECTURE.md か実装の**どちらかを必ず直す**。
- トレードオフは**正直に明示**してから決める(例: headless 廃止で「毎回 worker が要る」ことを承知の上で選んだ)。
- 近めの `todo` と将来 `future`、統合準備 `merge-prep` をラベルで分離して、issue を根拠が出てから昇格(最小主義)。

---

## 付録

**マージ済み PR**: #1(funnel強化) / #2(gate分離+INSPIRE) / #22(AGENTS) / #26(memory) / #28(dual完全統一)。
**ファイル構成**: `orchestrate.py` / `config.json` / `prompts/` / `worker/INSTRUCTIONS.md` / `tools/{start-worker.ps1,mock_worker.py}` / `memory/{README.md,preferences.md,(decisions.jsonl,seen.jsonl)}` / `ARCHITECTURE.md` / `AGENTS.md` / `README.md`。`runs/`・`queue/`・`memory/seen.jsonl` は gitignore。
**program_creater との関係**: 本 repo は #6–#9(ローカルAIレビュー / reviewer / PR連携 / multi-agent)の試作。最終的に `core/`(エンジン)・`domains/research/`(レンズ・契約・Tier)・`selection/`(funnel)・`memory/` へ畳む(AGENTS.md マージ地図、merge-prep issue #24/#25)。
