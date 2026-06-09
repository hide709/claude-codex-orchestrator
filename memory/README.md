# memory/ — cross-run memory (Issue #23)

run をまたいで再利用する知識。program_creater の `memory/` に対応(将来そこへ畳む)。

- **`decisions.jsonl`** — 人間が記録した採用/却下(正本・commit)。`promote`/`reject` で追記。
  各行: `{date, kind: promote|reject, run, id, hypothesis, note}`
- **`preferences.md`** — 研究者の好み・方針(commit)。`prefer "..."` で追記。生成時に尊重される。
- **`seen.jsonl`** — 自動。過去候補の**重複検知用**(gitignore・使い捨て)。kill には使わない。

## 書き方(人間)

```powershell
python orchestrate.py promote <run_id> <cand_id> --note "追求する理由"
python orchestrate.py reject  <run_id> <cand_id> --note "死んだ線の理由"
python orchestrate.py prefer  "高novelty優先 / ビームダイナミクス系統に注力"
```

## どう効くか
- **生成**: 却下済みの線を再提案しない / 好みに寄せる / 既出と重複しない角度を出す。
- **検証**: 人間が却下した類似線を verifier に渡し、再tread を novelty で見抜きやすくする。
- **重複検知**: 過去候補と似た案に `runs/<id>/REPORT.md` で印を付ける(検知のみ・棄却しない)。

## 原則
- 永続するのは **人間が採用/却下した知識だけ**。AI の判断で自動棄却はしない(ARCHITECTURE §0)。
- `seen.jsonl` は重複『検知』のみ。候補を落とすのは客観 hard gate(形不備)と人間だけ。
