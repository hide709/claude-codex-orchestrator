#!/usr/bin/env python3
"""
orchestrate.py — IDEA-stage funnel MVP   (see ARCHITECTURE.md §11)

研究の種を1件:
    発散(独立) -> red-team(攻撃->検証可能項目) -> Tier0検証 -> hard gate -> arbiter(整理)
で回し、Research Hypothesis Contract と decision_matrix を「成果物」として残す。

原則 (ARCHITECTURE §0):
  - AI は候補を出すだけ。裁くのは客観検証(形/文献/計算/soundness)。決めるのは人間。
  - orchestrator(このスクリプト)は LLM ではない。ループ・型強制・gate・集計のみ。
  - VERIFIER は生成者と別呼び出し(独立)。red-team は judge しない(attack -> convert)。
  - soft score を単一値に潰さない。落選は捨て案台帳へ(消さない)。

依存: Python 標準ライブラリのみ。engine は常駐 worker + queue 経由で呼ぶ。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"
MEMORY_DIR = ROOT / "memory"
QUEUE_DIR = ROOT / "queue"

# ----------------------------------------------------------------------------
# 発散レンズ (ARCHITECTURE §3.3 / 研究向け)
# ----------------------------------------------------------------------------
LENS_DESC = {
    "analogy":       "別分野の手法・結果を、この問題へ転用する角度から発想せよ。",
    "anomaly":       "既知の tension / excess / 未説明の観測を説明しうる仮説を立てよ。",
    "method-driven": "新しい手法・装置・データセットが可能にする『新しい測定』を起点に発想せよ。",
    "contrarian":    "広く信じられている前提を1つ『偽』と仮定し、そこから何が導かれるかを発想せよ。",
    "gap":           "パラメータ空間/測定の未探索の隅、未測定のレジームを狙って発想せよ。",
    "combination":   "既存の2つのアイデア・技術を掛け合わせた新しい仮説を立てよ。",
}

# ----------------------------------------------------------------------------
# スキーマ (strict structured output 互換)
#   * 原則すべての property を required にし、N/A は空文字/空配列で返させる。
#   * 例外: baseline / success_metric / failure_condition は任意(Issue #40 P3)。
#     形ゲート(required ベース)の対象外 = 後方互換。domain config 側で記入を促す。
# ----------------------------------------------------------------------------
def _obj(props, required):
    return {"type": "object", "additionalProperties": False,
            "required": required, "properties": props}

HYPOTHESIS_SCHEMA = _obj(
    {
        "id":                  {"type": "string"},
        "question":            {"type": "string"},
        "hypothesis":          {"type": "string"},
        "novelty_claim":       {"type": "string"},
        "soundness":           {"type": "string"},
        "falsification":       {"type": "string"},
        "test_method":         {"type": "string"},
        "feasibility":         {"type": "string"},
        "significance_if_true":{"type": "string"},
        "risk_type":           {"type": "string",
                                "enum": ["novelty", "soundness", "feasibility", "significance"]},
        "cheapest_kill":       {"type": "string"},
        "assumptions":         {"type": "array", "items": {"type": "string"}},
        "unknowns":            {"type": "array", "items": {"type": "string"}},
        # ---- 任意(比較・検証の明確化。研究種なら分野を問わず有効) ----
        "baseline":            {"type": "string"},
        "success_metric":      {"type": "string"},
        "failure_condition":   {"type": "string"},
        # 文献検索用の英語キーワード(契約本文が日本語でも evidence 検索を効かせる)
        "search_keywords":     {"type": "array", "items": {"type": "string"}},
    },
    ["id", "question", "hypothesis", "novelty_claim", "soundness", "falsification",
     "test_method", "feasibility", "significance_if_true", "risk_type",
     "cheapest_kill", "assumptions", "unknowns"],
)

REVIEW_SCHEMA = _obj(
    {"attacks": {"type": "array", "items": _obj(
        {
            "type":       {"type": "string",
                           "enum": ["hidden_assumption", "contradicting_work",
                                    "feasibility_hole", "confound", "stronger_variant"]},
            "claim":      {"type": "string"},
            "convert_to": {"type": "string",
                           "enum": ["assumption", "lit_check", "computation",
                                    "falsification_fix", "new_candidate"]},
            "pointer":    {"type": "string"},
        },
        ["type", "claim", "convert_to", "pointer"])}},
    ["attacks"],
)

_AXIS = _obj({"assessment": {"type": "string"},
              "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
             ["assessment", "confidence"])

DEFAULT_EVAL_AXES = ["novelty", "soundness", "feasibility", "significance"]

# 評価軸の説明(verifier プロンプトの追加軸用)。config の eval_axes_desc で上書き/追加できる。
AXIS_DESC = {
    "novelty": "新規性", "soundness": "整合性", "feasibility": "実現性", "significance": "重要性",
    "mechanism_clarity":  "物理・工学メカニズムが明確に述べられているか",
    "validation_clarity": "最初の検証(toy model/シミュレーション/データ)と成功・失敗条件が明確か",
    "baseline_clarity":   "比較すべき baseline が明確に定義されているか",
}


def verdict_schema(axes):
    """eval_axes(config 駆動)から verdict スキーマを生成。各軸は assessment+confidence。
    HEP は既定4軸、spacecraft 等は config で軸を足せる(soft score を単一値に潰さない原則は不変)。"""
    props = {ax: _AXIS for ax in axes}
    props.update({
        "prior_art": {"type": "array", "items": _obj(
            {"citation":    {"type": "string"},
             "source_tier": {"type": "string",
                             "enum": ["authoritative_db", "peer_reviewed", "preprint", "web"]},
             "relation":    {"type": "string"}},
            ["citation", "source_tier", "relation"])},
        "verdict":     {"type": "string", "enum": ["keep", "flag", "kill"]},
        "kill_reason": {"type": "string"},
        "notes":       {"type": "string"},
    })
    return _obj(props, list(axes) + ["prior_art", "verdict", "kill_reason", "notes"])


def extra_axes_text(axes, cfg):
    """core 4軸を超えるドメイン軸の説明を verifier プロンプトに渡す文。"""
    descs = {**AXIS_DESC, **cfg.get("eval_axes_desc", {})}
    extra = [a for a in axes if a not in DEFAULT_EVAL_AXES]
    return "\n".join(f"- **{a}** — {descs.get(a, a)}" for a in extra) if extra else "(なし)"


VERDICT_SCHEMA = verdict_schema(DEFAULT_EVAL_AXES)   # 既定(HEP)。verify は cfg["eval_axes"] で都度生成

# ----------------------------------------------------------------------------
# Runner 抽象 (engine を差し替え可能に)
# ----------------------------------------------------------------------------
class RunnerError(Exception):
    pass


def _extract_json(text):
    """codex の最終メッセージから JSON を取り出す(コードフェンス/前後ノイズに耐える)。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        return json.loads(text[s:e + 1])
    raise RunnerError("no JSON in model output")


def _launch(argv, stdin_text, timeout):
    """Windows の .cmd/.bat shim も吸収して subprocess 実行。"""
    exe = argv[0]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    return subprocess.run(argv, input=stdin_text, capture_output=True,
                          text=True, timeout=timeout, encoding="utf-8", errors="replace")


def _resolve_engine_exe(engine, cfg):
    """engine の実行ファイルを解決(config 上書き → PATH → 既定の場所)。見つからなければ None。"""
    p = cfg.get(f"{engine}_path")
    if p and Path(p).exists():
        return p
    w = shutil.which(engine)
    if w:
        return w
    cands = []
    if engine == "codex":
        cands = sorted((Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
    elif engine == "claude":
        cands = [Path.home() / "AppData" / "Roaming" / "SPB_Data" / ".local" / "bin" / "claude.exe",
                 Path.home() / ".local" / "bin" / "claude.exe"]
    for c in cands:
        if Path(c).exists():
            return str(c)
    return None


def engine_available(engine, cfg):
    return engine == "mock" or _resolve_engine_exe(engine, cfg) is not None


def _engine_argv(engine, exe, seed, cfg):
    """対話(非 headless)起動の argv。低フリクション(無確認・workspace書込)で seed prompt を渡す。"""
    if engine == "codex":
        # ネスト環境で codex の windows sandbox spawn が失敗するため、サンドボックスを bypass
        # (worker は queue/ 内の read/write のみ。orchestrator は信頼コード)
        return [exe, "-c", "service_tier=flex", "-c", f"model_reasoning_effort={cfg.get('reasoning_effort', 'low')}",
                "--dangerously-bypass-approvals-and-sandbox", seed]
    if engine == "claude":
        # skip-permissions: BypassPermissions 承認は初回一度きり(~/.claude.json に記録)。
        # 承認済みなら以降プロンプト無し。rename 等シェルも write-execute プレーン内は無確認。
        return [exe, "--dangerously-skip-permissions", seed]
    raise ValueError(f"unknown engine {engine}")


class InteractiveSessionRunner:
    """ADR-001: orchestrator が対話セッション(codex/claude)を **pywinpty(ConPTY)** で spawn・駆動する。
    headless(codex exec / claude -p)を使わない = サブスク内・**追加課金なし**。supervisor が job 単位で
    inject/observe し、完了は **report ファイル**で判定。LLM に daemon ループは持たせない。"""
    def __init__(self, engine, cfg):
        self.engine = engine
        self.cfg = cfg
        self.qin = QUEUE_DIR / engine / "inbox"
        self.qout = QUEUE_DIR / engine / "reports"
        self.qin.mkdir(parents=True, exist_ok=True)
        self.qout.mkdir(parents=True, exist_ok=True)
        self.exe = _resolve_engine_exe(engine, cfg)
        self.poll = cfg.get("queue_poll_sec", 3)
        self.timeout = cfg.get("queue_timeout_sec", cfg.get("timeout_sec", 600))
        self.warmup = cfg.get("session_warmup_sec", 8)
        self.enter_delay = cfg.get("inject_enter_delay_sec", 1.5)
        self.proc = None
        self._buf = []
        self._blk = threading.Lock()
        self._lock = threading.Lock()   # 1セッション=直列。job 注入が交錯しないように

    def _directive(self, label):
        # .tmp に書いてから rename(atomic)。orchestrator の途中読みを原理的に防ぐ。
        # rename はシェルだが claude=skip-permissions / codex=bypass で無確認に通る。
        return (f"queue/{self.engine}/inbox/{label}.json を読み、その prompt に従い schema に厳密準拠した "
                f"JSON だけを、まず queue/{self.engine}/reports/{label}.json.tmp に書いてから "
                f"queue/{self.engine}/reports/{label}.json へ rename してください。説明やコードフェンスは書かない。")

    def _spawn(self, seed):
        from winpty import PtyProcess  # 遅延 import(mock/offline は pywinpty 不要)
        if self.exe is None:
            raise RunnerError(f"{self.engine} の実行ファイルが見つからない(PATH/既定の場所に無い)")
        self.proc = PtyProcess.spawn(_engine_argv(self.engine, self.exe, seed, self.cfg),
                                     cwd=str(ROOT), dimensions=(50, 160))

        def _readloop():
            while True:
                try:
                    d = self.proc.read(4096)
                except Exception:
                    break
                if d:
                    with self._blk:
                        self._buf.append(d)
                else:
                    time.sleep(0.05)
        threading.Thread(target=_readloop, daemon=True).start()
        time.sleep(self.warmup)   # 起動待ち

    def run(self, prompt, schema, kind, label, logdir):
        with self._lock:
            inbox_f = self.qin / f"{label}.json"
            report_f = self.qout / f"{label}.json"
            tmp_f = self.qin / f".{label}.tmp"
            if report_f.exists():
                report_f.unlink()
            tmp_f.write_text(json.dumps(
                {"label": label, "kind": kind, "schema": schema, "prompt": prompt, "created": _now()},
                ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_f.replace(inbox_f)
            directive = self._directive(label)
            if self.proc is None:
                self._spawn(directive)                  # 最初の job は seed prompt として起動時に渡す
            else:
                self.proc.write(directive)              # 注入: テキストと Enter を別 write に分ける(ADR-001)
                time.sleep(self.enter_delay)
                self.proc.write("\r")
            waited = 0
            while waited < self.timeout:
                if report_f.exists():
                    raw = report_f.read_text(encoding="utf-8", errors="replace")
                    try:
                        data = _extract_json(raw)
                    except Exception:
                        time.sleep(self.poll)      # 途中書き込みの可能性 → 次ポーリングで再読込
                        waited += self.poll
                        continue
                    (logdir / f"{label}.log.txt").write_text(
                        f"[{self.engine}-session] {label}\n--- report ---\n{raw}", encoding="utf-8")
                    return data
                if self.proc is not None and not self.proc.isalive():
                    self._dump(logdir, label, "session が終了(report 未生成)")
                    raise RunnerError(f"{self.engine} session が終了(report 未生成) label={label}")
                time.sleep(self.poll)
                waited += self.poll
            self._dump(logdir, label, f"timeout {self.timeout}s")
            raise RunnerError(f"{self.engine} session timeout ({self.timeout}s) label={label}")

    def _dump(self, logdir, label, note):
        """診断用: セッション(TUI)出力をログに吐く。"""
        with self._blk:
            out = "".join(self._buf)
        try:
            (logdir / f"{label}.session.txt").write_text(
                f"[{self.engine}] {note}\n--- session output (last 6000) ---\n" + out[-6000:],
                encoding="utf-8")
        except Exception:
            pass

    def shutdown(self):
        if self.proc is not None:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass
            self.proc = None


class MockRunner:
    """LLM/トークン無しで配管を検証するためのダミー。--engine mock。"""
    def __init__(self, cfg=None):
        self.cfg = cfg

    def run(self, prompt, schema, kind, label, logdir):
        (logdir / f"{label}.log.txt").write_text(f"[mock] {label}\n{prompt[:400]}", encoding="utf-8")
        idx = int(re.search(r"(\d+)", label).group(1)) if re.search(r"(\d+)", label) else 0
        if kind == "hypothesis":
            lens = label.split("__")[-1]
            return {
                "id": label, "question": "（mock の問い）",
                "hypothesis": f"[{lens}] mock の反証可能な定量的仮説。",
                "novelty_claim": "最近接は mock et al.（要実検索）。",
                "soundness": "既知の保存則に反しない（mock）。",
                "falsification": "測定 X が閾値 Y を超えれば棄却。",
                "test_method": "toy MC / 公開データ D の再解析。",
                "feasibility": "必要統計は概算で実現範囲（mock）。",
                "significance_if_true": "もし正しければ Z が更新される。",
                "risk_type": ["novelty", "soundness", "feasibility", "significance"][idx % 4],
                "cheapest_kill": "既存データ D の1点チェックで反証可能。",
                "assumptions": ["前提A（mock）"], "unknowns": ["未知1（mock）"],
            }
        if kind == "review":
            return {"attacks": [
                {"type": "hidden_assumption", "claim": "前提Bが未明示（mock）。",
                 "convert_to": "assumption", "pointer": ""},
                {"type": "feasibility_hole", "claim": "背景Cを無視（mock）。",
                 "convert_to": "computation", "pointer": ""},
                {"type": "stronger_variant", "claim": "対象をWにすると新規性↑（mock）。",
                 "convert_to": "new_candidate", "pointer": ""},
            ]}
        if kind == "verdict":
            v = "kill" if idx == 1 else ("flag" if idx % 2 == 0 else "keep")  # 1件は kill で gate を検証
            # 軸は渡された schema から導出(eval_axes が config 駆動になったため / Issue #40)
            out = {ax: {"assessment": f"{ax} 良好（mock）",
                        "confidence": ["low", "medium"][i % 2]}
                   for i, (ax, p) in enumerate(schema.get("properties", {}).items())
                   if isinstance(p, dict) and "assessment" in p.get("properties", {})}
            out.update({
                "prior_art": [{"citation": "mock 2025", "source_tier": "preprint", "relation": "類似だが差分あり"}],
                "verdict": v,
                "kill_reason": "完全な先行事例あり（mock）" if v == "kill" else "",
                "notes": "mock verdict",
            })
            return out
        raise RunnerError(f"unknown kind {kind}")


_RUNNERS = {}
_RUNNERS_LOCK = threading.Lock()


def make_runner_for(engine, cfg):
    """engine→Runner。対話セッションは1 run 内で再利用するため engine 単位でキャッシュ(thread-safe)。"""
    with _RUNNERS_LOCK:   # 並列 generate で同一 engine の二重 spawn を防ぐ
        r = _RUNNERS.get(engine)
        if r is None:
            r = MockRunner(cfg) if engine == "mock" else InteractiveSessionRunner(engine, cfg)
            _RUNNERS[engine] = r
        return r


def shutdown_runners():
    """run 終了時に spawn 済みセッションを終了する。"""
    for r in _RUNNERS.values():
        fn = getattr(r, "shutdown", None)
        if fn:
            try:
                fn()
            except Exception:
                pass
    _RUNNERS.clear()


def usable_engines(engines, cfg):
    """使える engine を順序保持で返す。mock は常に可、それ以外は実行ファイルが解決できる場合のみ。"""
    return [e for e in engines if engine_available(e, cfg)]


# ----------------------------------------------------------------------------
# プロンプト
# ----------------------------------------------------------------------------
def render(template, **kw):
    for k, v in kw.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def load_prompt(name):
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------
# Cross-run memory  (program_creater の memory/ に対応 / Issue #23)
#   seen.jsonl       : 自動。重複『検知』のみ(kill しない)。gitignore。
#   decisions.jsonl  : 人間が promote/reject で記録(正本)。commit。
#   preferences.md   : 人間が prefer で記録。commit。
#   原則: 永続するのは人間が採用/却下した知識だけ。AI 判断で自動棄却はしない。
# ----------------------------------------------------------------------------
def _read_jsonl(path):
    out = []
    if path.exists():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[memory] skip malformed JSONL: {path}:{lineno}: {e}", file=sys.stderr)
    return out


def load_memory():
    MEMORY_DIR.mkdir(exist_ok=True)
    prefs = ""
    pfile = MEMORY_DIR / "preferences.md"
    if pfile.exists():
        prefs = "\n".join(l for l in pfile.read_text(encoding="utf-8").splitlines()
                          if l.strip().startswith("- ") and l.strip() != "- なし")
    return {"decisions": _read_jsonl(MEMORY_DIR / "decisions.jsonl"),
            "seen": _read_jsonl(MEMORY_DIR / "seen.jsonl"),
            "preferences": prefs.strip()}


def memory_digest(mem, max_each=8):
    """生成プロンプトに差し込む memory 要約。"""
    out = []
    if mem["preferences"]:
        out.append("## 研究者の好み(尊重する)\n" + mem["preferences"][:800])
    rej = [d for d in mem["decisions"] if d.get("kind") == "reject"][-max_each:]
    if rej:
        out.append("## 却下済みの線(再提案しない)\n" + "\n".join(
            f"- {d.get('hypothesis','')[:120]}" + (f"  (理由: {d['note']})" if d.get("note") else "")
            for d in rej))
    prom = [d for d in mem["decisions"] if d.get("kind") == "promote"][-max_each:]
    if prom:
        out.append("## 過去に採用した方向(重複は避け、隣接の新規性を狙う)\n" + "\n".join(
            f"- {d.get('hypothesis','')[:120]}" for d in prom))
    seen = mem["seen"][-max_each * 2:]
    if seen:
        out.append("## 既出の候補(重複を避ける)\n" + "\n".join(
            f"- {s.get('hypothesis','')[:100]}" for s in seen))
    return "\n\n".join(out) if out else "(まだ記憶なし)"


def memory_reject_hint(mem, max_items=6):
    """検証プロンプトに渡す『人間が却下済みの線』ヒント(再tread検出用。kill はしない)。"""
    rej = [d for d in mem["decisions"] if d.get("kind") == "reject"][-max_items:]
    if not rej:
        return ""
    return "[memory] 人間が過去に却下した線(再treadなら novelty を低めに・relation で明示):\n" + "\n".join(
        f"- {d.get('hypothesis','')[:120]}" + (f" (理由: {d['note']})" if d.get("note") else "") for d in rej)


def _bigrams(text):
    t = re.sub(r"\s+", "", str(text)).lower()
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


def _similarity(a, b):
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def mark_near_duplicates(cands, mem, threshold=0.5, max_seen=500):
    """過去 run の候補と似ていれば印を付ける(重複『検知』のみ。落とさない)。"""
    seen = mem["seen"][-max_seen:]
    for c in cands:
        text = c.get("question", "") + c.get("hypothesis", "")
        best_s, best = 0.0, None
        for s in seen:
            sim = _similarity(text, s.get("question", "") + s.get("hypothesis", ""))
            if sim > best_s:
                best_s, best = sim, s
        if best and best_s >= threshold:
            c["_near_dup"] = {"run": best.get("run"), "id": best.get("id"), "score": round(best_s, 2)}


def append_seen(run, cands):
    """この run の候補を seen.jsonl に追記(重複検知用の自動メモリ)。"""
    MEMORY_DIR.mkdir(exist_ok=True)
    with (MEMORY_DIR / "seen.jsonl").open("a", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps({
                "run": run["id"], "id": c["id"], "lens": c.get("_lens", ""),
                "question": c.get("question", ""), "hypothesis": c.get("hypothesis", ""),
                "verdict": (c.get("_verdict") or {}).get("verdict", ""),
                "llm_kill": bool(c.get("_llm_kill")), "created": run["created"],
            }, ensure_ascii=False) + "\n")


def _safe_component(value, label):
    text = str(value)
    if not text or text in (".", "..") or any(sep in text for sep in ("/", "\\")):
        raise ValueError(f"{label} にパス区切りや不正値は使えません: {value!r}")
    return text


def _load_candidate(run_id, cand_id):
    run_id = _safe_component(run_id, "run_id")
    cand_id = _safe_component(cand_id, "cand_id")
    base = ROOT / "runs" / run_id
    for sub in ("revised", "candidates"):
        p = base / sub / f"{cand_id}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def cmd_memory(kind, argv):
    """人間がメモリを書く CLI: promote/reject <run> <id> [--note] / prefer "text"。"""
    MEMORY_DIR.mkdir(exist_ok=True)
    if kind == "prefer":
        text = " ".join(argv).strip()
        if not text:
            print('使い方: python orchestrate.py prefer "好み・方針のテキスト"')
            sys.exit(1)
        pfile = MEMORY_DIR / "preferences.md"
        if not pfile.exists():
            pfile.write_text("# Preferences\n\n研究者の好み・方針(生成時に尊重)。\n\n", encoding="utf-8")
        with pfile.open("a", encoding="utf-8") as f:
            f.write(f"- ({_now()[:10]}) {text}\n")
        print("好みを記録 → memory/preferences.md")
        return
    ap = argparse.ArgumentParser(prog=f"orchestrate.py {kind}")
    ap.add_argument("run_id")
    ap.add_argument("cand_id")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    try:
        c = _load_candidate(a.run_id, a.cand_id)
    except ValueError as e:
        print(e)
        sys.exit(1)
    if not c:
        print(f"候補が見つからない: runs/{a.run_id}/(revised|candidates)/{a.cand_id}.json")
        sys.exit(1)
    rec = {"date": _now()[:10], "kind": kind, "run": a.run_id, "id": a.cand_id,
           "hypothesis": c.get("hypothesis", ""), "note": a.note}
    with (MEMORY_DIR / "decisions.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"記録: {kind} {a.run_id}/{a.cand_id} → memory/decisions.jsonl")


# ----------------------------------------------------------------------------
# 段階 (stages)
# ----------------------------------------------------------------------------
def planner(seed, constraints, cfg, run):
    """MVP の Planner は決定的: charter を固定するだけ(LLM 不使用)。"""
    charter = {
        "seed": seed,
        "constraints": constraints,
        "domain": cfg.get("domain", ""),
        "eval_axes": cfg["eval_axes"],
        "lenses": cfg["lenses"][: cfg["n_lenses"]],
        "engines": (cfg.get("engines", ["codex", "claude"]) if cfg["engine"] == "dual"
                    else [cfg["engine"]]),
        "lit_search_enabled": cfg.get("lit_search_enabled", True),
        "rounds": 1,
        "created": run["created"],
    }
    write_json(run, "charter.json", charter)
    return charter


def _git_commit():
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5, encoding="utf-8", errors="replace")
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def provenance(run, cfg, stage, label, **extra):
    data = {
        "stage": stage,
        "worker": label,
        "engine": cfg["engine"],
        "model": cfg["model"],
        "run_id": run["id"],
        "created": _now(),
        "repo_commit": run.get("commit", ""),
    }
    data.update(extra)
    return data


def generate(runner, charter, cfg, run, mem):
    """発散: レンズごとに独立生成(互いを見ない)。並列。memory で重複回避・好み反映。
    engines が複数なら engine をレンズに割り当てる(Codex + Claude Code 両方 = default)。"""
    tmpl = load_prompt("ideator")
    lenses = charter["lenses"]
    lens_desc_map = {**LENS_DESC, **cfg.get("lens_desc", {})}   # config で説明を上書き/追加(P1)
    digest = memory_digest(mem)

    engines = list(charter.get("engines") or ["mock"])   # main で live なものに絞り済み
    assign = [engines[i % len(engines)] for i in range(len(lenses))]
    fallback = engines[0]

    runner_cache = {}

    def get_runner(eng):
        if eng not in runner_cache:
            runner_cache[eng] = make_runner_for(eng, cfg)
        return runner_cache[eng]

    def one(i, lens):
        label = f"{run['id']}__gen_{i:02d}__{lens}"
        eng = assign[i]
        prompt = render(tmpl, seed=charter["seed"], constraints=charter["constraints"],
                        lens=lens, lens_desc=lens_desc_map.get(lens, lens), memory=digest,
                        domain_note=cfg.get("seed_charter_note", ""),
                        schema=json.dumps(HYPOTHESIS_SCHEMA, ensure_ascii=False))
        used = eng
        used_label = label
        try:
            data = get_runner(eng).run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", label, run["log"])
        except Exception as e:
            if eng != fallback:   # 失敗したら別の live engine にフォールバック
                log(run, f"  [gen {lens}] {eng} 失敗({e}) → {fallback} で再試行")
                try:
                    used_label = f"{label}__fallback_{fallback}"
                    data = get_runner(fallback).run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", used_label, run["log"])
                    used = f"{fallback}(fallback<{eng})"
                except Exception as e2:
                    log(run, f"  [gen {lens}] {fallback} も失敗: {e2}")
                    return None
            else:
                log(run, f"  [gen {lens}] FAILED: {e}")
                return None
        data["id"] = f"rq-{i:02d}"
        data["_lens"] = lens
        data["_engine"] = used
        data["provenance"] = provenance(run, cfg, "generate", used_label, lens=lens, engine=used)
        return data

    cands = _parallel(cfg, [(one, (i, lens)) for i, lens in enumerate(lenses)])
    cands = [c for c in cands if c]
    mark_near_duplicates(cands, mem)            # 過去 run との重複を『検知』(落とさない)
    for c in cands:
        write_json(run, f"candidates/{c['id']}.json", c)
    ndup = sum(1 for c in cands if c.get("_near_dup"))
    by_eng = {}
    for c in cands:
        by_eng[c.get("_engine", "?")] = by_eng.get(c.get("_engine", "?"), 0) + 1
    breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(by_eng.items()))
    log(run, f"  生成: {len(cands)}/{len(lenses)} 候補 ({breakdown})" +
        (f" / 過去と類似 {ndup}件(重複注意)" if ndup else ""))
    return cands


def redteam(runner, cands, cfg, run):
    """cross red-team: 攻撃 -> 検証可能項目に変換。judge しない。"""
    tmpl = load_prompt("redteam")
    extra_checks = "\n".join(f"- {x}" for x in cfg.get("redteam_extra_checks", [])) or "(なし)"

    def one(c):
        label = f"{run['id']}__review_{c['id']}"
        # blind: 著者(レンズ)情報は渡さない
        shown = {k: v for k, v in c.items()
                 if not k.startswith("_") and k not in ("id", "provenance")}
        prompt = render(tmpl, candidate=json.dumps(shown, ensure_ascii=False, indent=2),
                        extra_checks=extra_checks,
                        schema=json.dumps(REVIEW_SCHEMA, ensure_ascii=False))
        try:
            return c["id"], runner.run(prompt, REVIEW_SCHEMA, "review", label, run["log"])
        except Exception as e:
            log(run, f"  [review {c['id']}] FAILED: {e}")
            return c["id"], {"attacks": []}

    results = dict(_parallel(cfg, [(one, (c,)) for c in cands]))
    variants = []        # stronger_variant: 未追跡として未解決へ
    for c in cands:
        rv = results.get(c["id"], {"attacks": []})
        rv["provenance"] = provenance(run, cfg, "redteam", f"{run['id']}__review_{c['id']}", target=c["id"])
        c["_review"] = rv
        write_json(run, f"reviews/{c['id']}.json", rv)
        todo = []
        for a in rv.get("attacks", []):
            ct = a.get("convert_to")
            if ct == "assumption":
                c.setdefault("assumptions", []).append("[red-team] " + a["claim"])
            elif ct in ("lit_check", "computation", "falsification_fix", "confound"):
                todo.append(f"({ct}) {a['claim']}" + (f"  -> {a['pointer']}" if a.get("pointer") else ""))
            elif ct == "new_candidate":
                variants.append({"from": c["id"], "claim": a["claim"]})
        c["_verify_todo"] = todo
    run["variants"] = variants
    log(run, f"  red-team: 攻撃を検証項目へ変換 / stronger_variant {len(variants)}件は未解決へ")
    return cands


def revise(runner, cands, cfg, run):
    """red-team の攻撃を受けて、自案を1回だけ改訂する。原案は candidates/ に残す。"""
    tmpl = load_prompt("revise")

    def one(c):
        label = f"{run['id']}__revise_{c['id']}"
        shown = {k: v for k, v in c.items()
                 if not k.startswith("_") and k not in ("provenance",)}
        prompt = render(tmpl,
                        candidate=json.dumps(shown, ensure_ascii=False, indent=2),
                        review=json.dumps(c.get("_review", {"attacks": []}), ensure_ascii=False, indent=2),
                        schema=json.dumps(HYPOTHESIS_SCHEMA, ensure_ascii=False))
        try:
            data = runner.run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", label, run["log"])
            data["id"] = c["id"]
            data["_lens"] = c.get("_lens", "?")
            data["_engine"] = c.get("_engine", "?")   # 生成 engine の印を改訂後にも引き継ぐ
            data["_review"] = c.get("_review", {"attacks": []})
            data["_verify_todo"] = c.get("_verify_todo", [])
            if c.get("_near_dup"):
                data["_near_dup"] = c["_near_dup"]   # 重複検知の印を改訂後にも引き継ぐ
            data["provenance"] = provenance(run, cfg, "revise", label,
                                            lens=c.get("_lens", "?"), revised_from=c["id"])
            return data
        except Exception as e:
            log(run, f"  [revise {c['id']}] FAILED: {e} / 原案を継続")
            return c

    revised = _parallel(cfg, [(one, (c,)) for c in cands])
    for c in revised:
        write_json(run, f"revised/{c['id']}.json", c)
    log(run, f"  revise: {len(revised)} 候補を改訂済みとして保存")
    return revised


def _search_terms(text, max_terms):
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "into", "using", "use",
        "has", "have", "are", "was", "were", "can", "could", "would", "should",
        "study", "research", "method", "effect", "data", "analysis", "test",
        "off", "non", "via", "per",   # ON/OFF 等から混入するノイズ語(NTRS は AND 検索なので致命的)
    }
    terms = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{2,}", text):
        t = tok.strip("._-").lower()
        if len(t) < 3 or t in stop:
            continue
        if t not in terms:
            terms.append(t)
        if len(terms) >= max_terms:
            break
    return terms


def _candidate_terms(c, cfg):
    """文献検索語: 候補自身の search_keywords(英語・LLM 生成)を優先。
    無ければ本文から ASCII 抽出(従来)— 日本語契約だとジャーゴンしか拾えずリコールが落ちる。"""
    kws = []
    for kw in (c.get("search_keywords") or []):
        for tok in str(kw).split():
            t = tok.strip().lower()
            if len(t) >= 2 and t not in kws:
                kws.append(t)
    if kws:
        return kws[: int(cfg.get("lit_search_max_terms", 6))]
    text = " ".join(str(c.get(k, "")) for k in
                    ("question", "hypothesis", "novelty_claim", "test_method"))
    return _search_terms(text, int(cfg.get("lit_search_max_terms", 6)))


def collect_prior_art(c, cfg, run):
    """Tier0 novelty の補助: arXiv API から候補文献を取り、artifact として残す。"""
    if not cfg.get("lit_search_enabled", True):
        data = {"enabled": False, "query": "", "results": [], "error": ""}
        write_json(run, f"evidence/{c['id']}.arxiv.json", data)
        return data

    terms = _candidate_terms(c, cfg)
    if not terms:
        data = {"enabled": True, "query": "", "results": [], "error": "no_ascii_search_terms"}
        write_json(run, f"evidence/{c['id']}.arxiv.json", data)
        return data

    query = " OR ".join(f"all:{t}" for t in terms)
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": 0,
        "max_results": int(cfg.get("lit_search_max_results", 5)),
        "sortBy": "relevance",
        "sortOrder": "descending",
    })
    url = f"https://export.arxiv.org/api/query?{params}"
    data = {"enabled": True, "query": query, "url": url, "results": [], "error": ""}
    try:
        timeout = int(cfg.get("lit_search_timeout_sec", 15))
        req = urllib.request.Request(url, headers={"User-Agent": "claude-codex-orchestrator/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        root = ET.fromstring(body)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
            arxiv_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
            authors = []
            for author in entry.findall("atom:author", ns)[:5]:
                name = author.findtext("atom:name", default="", namespaces=ns)
                if name:
                    authors.append(name)
            data["results"].append({
                "citation": f"{', '.join(authors)} ({published[:4]}), {title}",
                "source_tier": "preprint",  # arXiv はプレプリント(査読前)。INSPIRE/査読より下位
                "relation": "arXiv search candidate; verifier must assess relation",
                "url": arxiv_id,
            })
    except Exception as e:
        data["error"] = str(e)
    write_json(run, f"evidence/{c['id']}.arxiv.json", data)
    return data


def collect_inspire(c, cfg, run):
    """Tier0 novelty 補助: INSPIRE-HEP(HEP の権威DB)から候補文献を取り artifact 化。
    inspire_mode(Issue #40 P4): always(HEP 既定) | trigger(候補文に trigger 語がある時だけ
    cross-domain hint として引く。spacecraft 等の隣接分野用) | off。
    未指定なら従来の inspire_enabled から導出(後方互換)。"""
    mode = cfg.get("inspire_mode") or ("always" if cfg.get("inspire_enabled", True) else "off")
    if mode == "off":
        data = {"enabled": False, "query": "", "results": [], "error": ""}
        write_json(run, f"evidence/{c['id']}.inspire.json", data)
        return data
    text = " ".join(str(c.get(k, "")) for k in
                    ("question", "hypothesis", "novelty_claim", "test_method"))
    if mode == "trigger":
        low = text.lower()
        trig = [str(t).lower() for t in cfg.get("inspire_trigger_terms", [])]
        if not any(t in low for t in trig):
            data = {"enabled": False, "query": "", "results": [],
                    "error": "", "skipped": "no_trigger_terms"}
            write_json(run, f"evidence/{c['id']}.inspire.json", data)
            return data
    terms = _candidate_terms(c, cfg)
    if not terms:
        data = {"enabled": True, "query": "", "results": [], "error": "no_ascii_search_terms"}
        write_json(run, f"evidence/{c['id']}.inspire.json", data)
        return data
    q = " ".join(terms[:5])  # INSPIRE は free-text。広すぎる OR を避け上位語のみ
    params = urllib.parse.urlencode({
        "q": q,
        "size": int(cfg.get("lit_search_max_results", 5)),
        "fields": "titles,authors.full_name,arxiv_eprints,earliest_date",
        "sort": "mostrecent",
    })
    url = f"https://inspirehep.net/api/literature?{params}"
    data = {"enabled": True, "query": q, "url": url, "results": [], "error": ""}
    try:
        timeout = int(cfg.get("lit_search_timeout_sec", 15))
        req = urllib.request.Request(
            url, headers={"User-Agent": "claude-codex-orchestrator/0.1", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        for hit in body.get("hits", {}).get("hits", []):
            m = hit.get("metadata", {})
            title = (m.get("titles") or [{}])[0].get("title", "")
            authors = [a.get("full_name", "") for a in (m.get("authors") or [])[:5] if a.get("full_name")]
            year = (m.get("earliest_date", "") or "")[:4]
            eprint = (m.get("arxiv_eprints") or [{}])[0].get("value", "")
            rel = ("INSPIRE-HEP cross-domain hint (trigger mode); verifier must assess relation"
                   if mode == "trigger" else
                   "INSPIRE-HEP search candidate; verifier must assess relation")
            data["results"].append({
                "citation": f"{', '.join(authors)} ({year}), {title}".strip(),
                "source_tier": "authoritative_db",  # INSPIRE は査読・curated メタデータの権威DB
                "relation": rel,
                "url": (f"https://arxiv.org/abs/{eprint}" if eprint else ""),
            })
    except Exception as e:
        data["error"] = str(e)
    write_json(run, f"evidence/{c['id']}.inspire.json", data)
    return data


def collect_ntrs(c, cfg, run):
    """Tier0 novelty 補助: NASA NTRS(航空宇宙の curated STI リポジトリ)から候補文献を取り artifact 化。
    metadata-only(full text は取らない / Issue #40 P4)。spacecraft domain の primary provider。"""
    if not cfg.get("ntrs_enabled", False):
        data = {"enabled": False, "query": "", "results": [], "error": ""}
        write_json(run, f"evidence/{c['id']}.ntrs.json", data)
        return data
    terms = _candidate_terms(c, cfg)
    if not terms:
        data = {"enabled": True, "query": "", "results": [], "error": "no_ascii_search_terms"}
        write_json(run, f"evidence/{c['id']}.ntrs.json", data)
        return data
    n = int(cfg.get("lit_search_max_results", 5))
    url = "https://ntrs.nasa.gov/api/citations/search"

    def _post(query):
        timeout = int(cfg.get("lit_search_timeout_sec", 15))
        payload = json.dumps({"q": query, "page": {"size": n, "from": 0}}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"User-Agent": "claude-codex-orchestrator/0.1",
                     "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    # NTRS は全語 AND(OR 構文なし)。語を絞らないとリコール不足(実測)。
    # 4語で 0 件なら上位2語で1回だけ再検索(recall fallback)。
    q = " ".join(terms[:4])
    data = {"enabled": True, "query": q, "url": url, "results": [], "error": ""}
    try:
        hits = list(_post(q).get("results") or [])
        # 結果が薄ければ上位2語で広げ、id で重複排除してマージ(AND 検索はリコールが落ちやすい)
        if len(hits) < 2 and len(terms) > 2:
            q2 = " ".join(terms[:2])
            data["query"] = f"{q} | fallback: {q2}"
            seen_ids = {h.get("id") for h in hits}
            hits += [h for h in (_post(q2).get("results") or []) if h.get("id") not in seen_ids]
        for hit in hits[:n]:
            title = " ".join(str(hit.get("title", "")).split())
            authors = []
            for aa in (hit.get("authorAffiliations") or [])[:5]:
                name = (((aa.get("meta") or {}).get("author") or {}).get("name", ""))
                if name:
                    authors.append(name)
            pubs = hit.get("publications") or [{}]
            year = (str(pubs[0].get("publicationDate", "")) or str(hit.get("distributionDate", "")))[:4]
            sti = hit.get("stiType", "")
            cid = hit.get("id", "")
            abstract = " ".join(str(hit.get("abstract", "")).split())[:300]
            data["results"].append({
                "citation": f"{', '.join(authors)} ({year}), {title}" + (f" [NTRS:{sti}]" if sti else ""),
                "source_tier": "authoritative_db",  # NASA の curated STI リポジトリ
                "relation": "NASA NTRS search candidate; verifier must assess relation",
                "url": (f"https://ntrs.nasa.gov/citations/{cid}" if cid else ""),
                "abstract_snippet": abstract,
            })
    except Exception as e:
        data["error"] = str(e)
    write_json(run, f"evidence/{c['id']}.ntrs.json", data)
    return data


def collect_evidence(c, cfg, run):
    """Tier0 novelty 補助の集約: arXiv(preprint) + INSPIRE-HEP + NASA NTRS(authoritative_db)。
    どれを引くかは config 駆動(inspire_mode / ntrs_enabled — Issue #40)。
    §8 Read プレーン: 許可した read-only API のみ叩き、結果はスナップショット保存。"""
    if not cfg.get("lit_search_enabled", True):
        empty = {"enabled": False, "query": "", "results": [], "error": ""}
        for prov in ("arxiv", "inspire", "ntrs"):
            write_json(run, f"evidence/{c['id']}.{prov}.json", empty)
        return {"arxiv": empty, "inspire": empty, "ntrs": empty}
    return {"arxiv": collect_prior_art(c, cfg, run),
            "inspire": collect_inspire(c, cfg, run),
            "ntrs": collect_ntrs(c, cfg, run)}


def evidence_refs(c):
    return [f"evidence/{c['id']}.{prov}.json" for prov in ("arxiv", "inspire", "ntrs")]


def verify(runner, cands, cfg, run, mem):
    """Tier0 検証: 形(決定的) + 文献/soundness/feasibility(独立な検証呼び出し)。"""
    tmpl = load_prompt("verifier")
    required = HYPOTHESIS_SCHEMA["required"]
    axes = cfg.get("eval_axes", DEFAULT_EVAL_AXES)
    vschema = verdict_schema(axes)
    extra_axes = extra_axes_text(axes, cfg)

    def form_ok(c):
        missing = [k for k in required if k in ("assumptions", "unknowns")
                   and not c.get(k) or (k not in ("assumptions", "unknowns") and not str(c.get(k, "")).strip())]
        # cheapest_kill と falsification は特に重視
        for k in ("hypothesis", "falsification", "cheapest_kill", "test_method"):
            if not str(c.get(k, "")).strip():
                missing.append(k)
        return sorted(set(missing))

    def one(c):
        label = f"{run['id']}__verify_{c['id']}"
        miss = form_ok(c)
        if miss:
            return c["id"], {"_form_fail": miss, "verdict": "kill",
                             "kill_reason": f"形不備(必須欠落): {', '.join(miss)}"}
        prior_art_hint = collect_evidence(c, cfg, run)
        todo_items = list(c.get("_verify_todo", []))
        _rej = memory_reject_hint(mem)
        if _rej:
            todo_items.append(_rej)
        prompt = render(tmpl,
                        candidate=json.dumps({k: v for k, v in c.items() if not k.startswith("_")},
                                             ensure_ascii=False, indent=2),
                        todo="\n".join(todo_items) or "(なし)",
                        extra_axes=extra_axes,
                        prior_art_hint=json.dumps(prior_art_hint, ensure_ascii=False, indent=2),
                        schema=json.dumps(vschema, ensure_ascii=False))
        try:
            verdict = runner.run(prompt, vschema, "verdict", label, run["log"])
            verdict["provenance"] = provenance(run, cfg, "verify", label, target=c["id"])
            verdict["evidence_refs"] = evidence_refs(c)
            return c["id"], verdict
        except Exception as e:
            log(run, f"  [verify {c['id']}] FAILED: {e}")
            fb = {"verdict": "flag", "kill_reason": "",
                  "notes": f"検証エラー(要再実行): {e}", "prior_art": [],
                  "provenance": provenance(run, cfg, "verify", label, target=c["id"]),
                  "evidence_refs": evidence_refs(c)}
            for ax in axes:
                fb[ax] = {"assessment": "未検証", "confidence": "low"}
            return c["id"], fb

    verdicts = dict(_parallel(cfg, [(one, (c,)) for c in cands]))
    for c in cands:
        c["_verdict"] = verdicts.get(c["id"])
        write_json(run, f"verdicts/{c['id']}.json", c["_verdict"])
    return cands


def hard_gate(cands, run):
    """**客観的な不備(形=必須欠落)だけ**を自動 reject。
    LLM の verdict='kill' は『判断』であって客観事実ではないので落とさず、人間確認用に印を付けて
    survivor に残す(ARCHITECTURE §3.6/§9: hard gate は客観のみ。AIに採用可否を裁かせない)。"""
    survivors, discarded = [], []
    for c in cands:
        v = c.get("_verdict", {})
        if v.get("_form_fail"):                       # 客観: 形不備のみ自動 reject
            discarded.append((c, v.get("kill_reason", "(理由未記載)")))
            continue
        if v.get("verdict") == "kill":                # LLM の kill = 推奨。落とさず人間確認へ
            c["_llm_kill"] = True
            c["_llm_kill_reason"] = v.get("kill_reason", "")
        survivors.append(c)

    lines = ["# 捨て案台帳 (discarded)\n",
             "**客観的な不備(形=必須項目の欠落)のみ**を自動 reject(ARCHITECTURE §3.6)。消さずに理由付きで残す。",
             "> LLM が kill 推奨した候補はここには入れない。`decision_matrix` に "
             "`kill?(LLM/要確認)` として残し、人間が棄却の妥当性を判断する(誤kill救済)。\n"]
    for c, reason in discarded:
        lines += [f"## {c['id']}  (lens: {c.get('_lens','?')})",
                  f"- 仮説: {c.get('hypothesis','')}", f"- 棄却理由(客観): **{reason}**", ""]
    if not discarded:
        lines.append("_(今回 客観 hard gate で落ちた候補は無し)_\n")
    write_text(run, "discarded.md", "\n".join(lines))
    n_llm_kill = sum(1 for c in survivors if c.get("_llm_kill"))
    log(run, f"  hard gate: 生存 {len(survivors)}(内 LLM-kill推奨 {n_llm_kill}=要人間確認) / 客観棄却 {len(discarded)}")
    return survivors


def arbiter(survivors, run, axes=None):
    """idea段の Arbiter = 整理係。勝者を選ばず matrix を人間に出す。
    評価軸は config の eval_axes 駆動(P0: 以前は4軸ハードコードだった)。"""
    axes = list(axes or DEFAULT_EVAL_AXES)

    def cell(v, axis):
        a = v.get(axis, {})
        txt = str(a.get("assessment", "-")).replace("\n", " ").replace("|", "/")
        if len(txt) > 90:
            txt = txt[:90].rstrip() + "…"   # 全文は verdicts/*.json に残す
        return f"{txt} [{a.get('confidence','-')}]"

    rows = []
    for c in survivors:
        v = c.get("_verdict", {})
        status = "kill?(LLM/要確認)" if c.get("_llm_kill") else v.get("verdict", "?")
        row = {
            "id": c["id"], "lens": c.get("_lens", "?"), "engine": c.get("_engine", "?"),
            "verdict": status, "risk_type": c.get("risk_type", ""),
            "hypothesis": c.get("hypothesis", ""),
            "cheapest_kill": c.get("cheapest_kill", ""),
        }
        for ax in axes:
            row[ax] = cell(v, ax)
        rows.append(row)
    # 透明な並べ替え: keep→flag→kill?(LLM)。同順位は high/medium confidence 数(単一スコアに潰さない)
    def score(r):
        order = {"keep": 0, "flag": 1}.get(r["verdict"], 2)   # kill?(LLM/要確認) は 2
        strong = sum(("high" in r[ax] or "medium" in r[ax]) for ax in axes)
        return (order, -strong)
    rows.sort(key=score)
    write_json(run, "decision_matrix.json", rows)

    header = "| id | lens | engine | verdict | risk | " + " | ".join(axes) + " | cheapest_kill |"
    sep = "|" + "---|" * (6 + len(axes))
    md = ["# decision_matrix (人間が読む)\n",
          "**勝者は選んでいない。** AIは候補を出し客観検証しただけ。",
          "どの生存案に *実験予算* を割くかは人間が決める(ARCHITECTURE §3.7 / §5)。\n",
          "verdict 凡例: `keep` / `flag`(通説違反など要注目で残す) / "
          "`kill?(LLM/要確認)`=LLM は kill 推奨だが客観未確認 → 人間が棄却の妥当性を判断。\n",
          header, sep]
    for r in rows:
        cells = [r["id"], r["lens"], r["engine"], r["verdict"], r["risk_type"]]
        cells += [r[ax] for ax in axes]
        cells.append(r["cheapest_kill"])
        md.append("| " + " | ".join(str(x).replace("|", "/").replace("\n", " ") for x in cells) + " |")
    md.append("\n## 各候補の仮説\n")
    for r in rows:
        md.append(f"- **{r['id']}** ({r['lens']}): {r['hypothesis']}")
    write_text(run, "decision_matrix.md", "\n".join(md))
    return rows


def write_unresolved(cands, run):
    lines = ["# 未解決論点 (unresolved)\n", "## 未追跡の stronger_variant\n"]
    for v in run.get("variants", []):
        lines.append(f"- ({v['from']} 由来) {v['claim']}")
    if not run.get("variants"):
        lines.append("_(なし)_")
    lines.append("\n## 各候補の unknowns\n")
    for c in cands:
        for u in c.get("unknowns", []):
            lines.append(f"- ({c['id']}) {u}")
    write_text(run, "unresolved.md", "\n".join(lines))


def report(charter, survivors, all_cands, run):
    _eng = {}
    for c in all_cands:
        _eng[c.get("_engine", "?")] = _eng.get(c.get("_engine", "?"), 0) + 1
    eng_bd = ", ".join(f"{k}:{v}" for k, v in sorted(_eng.items())) or "-"
    md = [f"# RUN REPORT — {run['id']}\n",
          f"- seed: **{charter['seed']}**",
          f"- constraints: {charter['constraints'] or '(なし)'}",
          *([f"- domain: {charter['domain']}"] if charter.get("domain") else []),
          f"- engine: {run['engine']} / model: {run['model']}  (生成 engine 内訳: {eng_bd})",
          f"- 生成 {len(all_cands)} / 生存 {len(survivors)} / 客観棄却 {len(all_cands)-len(survivors)}"
          + (f" / 内 LLM-kill推奨 {sum(1 for c in survivors if c.get('_llm_kill'))}(要人間確認)"
             if any(c.get('_llm_kill') for c in survivors) else ""),
          f"- created: {run['created']}\n",
          *(["## ⚠ 過去 run との重複注意(検知のみ・棄却ではない)",
             *[f"- {c['id']}: 過去 {c['_near_dup']['run']}/{c['_near_dup']['id']} と類似 "
               f"(bigram {c['_near_dup']['score']})" for c in all_cands if c.get('_near_dup')], ""]
            if any(c.get('_near_dup') for c in all_cands) else []),
          "## 読み方",
          "1. `decision_matrix.md` … 生存候補を評価軸ごとに(単一スコアに潰さず)一覧。",
          "2. `candidates/*.json` … 各 Research Hypothesis Contract。",
          "3. `reviews/*.json` … red-team の攻撃(検証項目へ変換済み)。",
          "4. `revised/*.json` … red-team 後の改訂版。原案は `candidates/` に残る。",
          "5. `evidence/*.json` … arXiv(preprint)/INSPIRE-HEP/NASA NTRS(権威DB)から機械的に収集した検証補助データ。",
          "6. `verdicts/*.json` … Tier0 検証結果(novelty/soundness/feasibility + prior_art)。",
          "7. `discarded.md` … hard gate 落ち(理由付き)。 `unresolved.md` … 未解決・未追跡変種。",
          "",
          "## 次の一手(人間)",
          "- 生存案のうち `cheapest_kill` が安いものから Tier1(toy計算/既存データ再解析)に回す。",
          "- prior_art の source_tier が低い novelty 判定は、権威DB(INSPIRE等)で裏取りする。",
          "- flag(通説違反など)は消さず、面白い線として別途検討。",
          "- `kill?(LLM/要確認)` は LLM の棄却理由が客観的に正しいか人間が確認(誤kill救済)。",
          "- 採用/却下を memory に記録 → 次回生成へ反映: "
          "`python orchestrate.py promote <run> <id>` / `reject <run> <id> --note \"理由\"` / `prefer \"好み\"`",
          ""]
    write_text(run, "REPORT.md", "\n".join(md))


# ----------------------------------------------------------------------------
# 並列実行 / IO
# ----------------------------------------------------------------------------
def _parallel(cfg, jobs):
    out = []
    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
        futs = [ex.submit(fn, *args) for fn, args in jobs]
        for f in futs:
            out.append(f.result())
    return out


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(s):
    return re.sub(r"[^a-zA-Z0-9]+", "-", s.strip())[:40].strip("-").lower() or "run"


def new_run(seed, cfg):
    rid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + _slug(seed)
    base = ROOT / "runs" / rid
    (base / "log").mkdir(parents=True, exist_ok=True)
    for d in ("candidates", "reviews", "revised", "verdicts", "evidence"):
        (base / d).mkdir(exist_ok=True)
    return {"id": rid, "dir": base, "log": base / "log", "created": _now(),
            "engine": cfg["engine"], "model": cfg["model"], "commit": _git_commit(), "_logbuf": []}


def write_json(run, rel, obj):
    p = run["dir"] / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(run, rel, text):
    (run["dir"] / rel).write_text(text, encoding="utf-8")


def log(run, msg):
    print(msg, flush=True)
    run["_logbuf"].append(msg)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
DEFAULT_CFG = {
    "engine": "dual", "engines": ["codex", "claude"], "model": "gpt-5.5", "reasoning_effort": "low",
    "service_tier": "flex", "concurrency": 4, "n_lenses": 4, "timeout_sec": 420,
    "lenses": ["analogy", "anomaly", "method-driven", "contrarian", "gap", "combination"],
    "eval_axes": ["novelty", "soundness", "feasibility", "significance"],
    "lit_search_enabled": True, "lit_search_max_terms": 6,
    "lit_search_max_results": 5, "lit_search_timeout_sec": 15,
    "inspire_enabled": True,   # inspire_mode(always|trigger|off)未指定時はここから導出
    "ntrs_enabled": False,     # NASA NTRS(spacecraft domain で有効化)
    "queue_poll_sec": 3, "queue_timeout_sec": 600,
    "session_warmup_sec": 8, "inject_enter_delay_sec": 1.5,
}


def load_cfg(path):
    cfg = dict(DEFAULT_CFG)
    if path and Path(path).exists():
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows cp932 でも Unicode で落ちないように
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # memory 書き込み用サブコマンド(人間が採用/却下/好みを記録)
    if len(sys.argv) > 1 and sys.argv[1] in ("promote", "reject", "prefer"):
        return cmd_memory(sys.argv[1], sys.argv[2:])

    ap = argparse.ArgumentParser(description="IDEA-stage funnel MVP (ARCHITECTURE §11)")
    ap.add_argument("--seed", help="研究の種(問い or hunch)")
    ap.add_argument("--seed-file", help="種をファイルから読む")
    ap.add_argument("--constraints", default="", help="制約(使える装置/データ/計算資源 等)")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--engine", choices=["dual", "codex", "claude", "mock"], help="config を上書き")
    ap.add_argument("--n-lenses", type=int, help="使う発散レンズ数(config を上書き)")
    ap.add_argument("--no-lit-search", action="store_true",
                    help="文献検索(arXiv/INSPIRE)を無効化(offline/高速テスト用)")
    ap.add_argument("--engines", help='生成 engine をカンマ区切りで上書き(例: "codex,claude" / "codex")')
    ap.add_argument("--timeout", type=int, help="queue_timeout_sec(1 job の応答待ち上限・秒)を上書き")
    args = ap.parse_args()

    seed = args.seed or (Path(args.seed_file).read_text(encoding="utf-8").strip()
                         if args.seed_file else None)
    if not seed:
        ap.error("--seed か --seed-file が必要です")

    cfg = load_cfg(args.config)
    if args.engine:
        cfg["engine"] = args.engine
    if args.n_lenses:
        cfg["n_lenses"] = args.n_lenses
    if args.no_lit_search:
        cfg["lit_search_enabled"] = False
    if args.engines:
        es = [e.strip() for e in args.engines.split(",") if e.strip()]
        cfg["engines"] = es
        cfg["engine"] = "dual" if len(es) > 1 else (es[0] if es else cfg["engine"])
    if args.timeout:
        cfg["queue_timeout_sec"] = args.timeout

    run = new_run(seed, cfg)
    mem = load_memory()
    print(f"\n=== RUN {run['id']}  (engine={cfg['engine']}) ===")
    print(f"seed: {seed}")
    print(f"memory: 既出候補 {len(mem['seen'])} / 決定 {len(mem['decisions'])} / "
          f"好み {'有' if mem['preferences'] else '無'}\n")

    log(run, "[1/7] PLANNER  — charter 固定")
    charter = planner(seed, args.constraints, cfg, run)
    # 使える engine(実行ファイルが解決できるもの。mock は常に可)を確定。
    # セッションは orchestrator が pywinpty で spawn・駆動する(手動 worker 不要)。
    live = usable_engines(charter["engines"], cfg)
    if not live:
        print(f"使える engine がありません(要求: {charter['engines']})。")
        print("  codex / claude が PATH か既定の場所にあるか確認(新しいターミナルの PATH に入っているか)。")
        print("  配管だけ確認するなら: --engine mock")
        sys.exit(1)
    if live != list(charter["engines"]):
        log(run, f"  注意: 実行ファイル未解決の engine を除外 → 使用 engine {live}(要求 {charter['engines']})")
    charter["engines"] = live
    runner = make_runner_for(live[0], cfg)   # primary。セッションは初回 job で spawn される

    try:
        log(run, "[2/7] GENERATE — 発散(独立・並列・memory反映)")
        cands = generate(runner, charter, cfg, run, mem)
        if not cands:
            print("候補が0件。engine/認証/timeout を確認(--engine mock で配管だけ検証可)。")
            sys.exit(1)
        log(run, "[3/7] RED-TEAM — 攻撃 -> 検証項目へ変換")
        cands = redteam(runner, cands, cfg, run)
        log(run, "[4/7] REVISE   — 攻撃を受けて仮説を1回だけ改訂")
        cands = revise(runner, cands, cfg, run)
        log(run, "[5/7] VERIFY   — Tier0(形/文献/soundness/feasibility)")
        cands = verify(runner, cands, cfg, run, mem)
        log(run, "[6/7] HARD GATE— kill を捨て案台帳へ")
        survivors = hard_gate(cands, run)
        log(run, "[7/7] ARBITER  — 整理(勝者は選ばない)")
        arbiter(survivors, run, charter["eval_axes"])
        write_unresolved(cands, run)
        report(charter, survivors, cands, run)
        append_seen(run, cands)                     # 自動メモリ(重複検知用)に追記
        write_text(run, "run.log", "\n".join(run["_logbuf"]))
        print(f"\n✅ 完了 → {run['dir']}")
        print(f"   まず読む: {run['dir'] / 'REPORT.md'}  /  {run['dir'] / 'decision_matrix.md'}")
    finally:
        shutdown_runners()                          # spawn した対話セッションを終了


if __name__ == "__main__":
    main()
