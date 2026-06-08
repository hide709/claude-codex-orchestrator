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

依存: Python 標準ライブラリのみ。engine は codex CLI(claude は CLI があれば差し替え可)。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROMPTS_DIR = ROOT / "prompts"

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
# スキーマ (codex --output-schema 用 / strict structured output 互換)
#   * すべての property を required にし、N/A は空文字/空配列で返させる
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

VERDICT_SCHEMA = _obj(
    {
        "novelty":      _AXIS,
        "soundness":    _AXIS,
        "feasibility":  _AXIS,
        "significance": _AXIS,
        "prior_art": {"type": "array", "items": _obj(
            {"citation":    {"type": "string"},
             "source_tier": {"type": "string",
                             "enum": ["authoritative_db", "peer_reviewed", "preprint", "web"]},
             "relation":    {"type": "string"}},
            ["citation", "source_tier", "relation"])},
        "verdict":     {"type": "string", "enum": ["keep", "flag", "kill"]},
        "kill_reason": {"type": "string"},
        "notes":       {"type": "string"},
    },
    ["novelty", "soundness", "feasibility", "significance",
     "prior_art", "verdict", "kill_reason", "notes"],
)

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


def _resolve_codex(cfg):
    """codex 実行ファイルを解決。Windows では PATH に無くデスクトップ版 bin に居ることがある。"""
    cands = []
    if cfg.get("codex_path"):
        cands.append(Path(cfg["codex_path"]))
    bins = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    cands += sorted(bins.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    w = shutil.which("codex")
    if w:
        cands.append(Path(w))

    seen = set()
    for cand in cands:
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            proc = _launch([str(cand), "--version"], None, 5)
            if proc.returncode == 0:
                return str(cand)
        except Exception:
            continue
    if cands:
        return str(cands[0])
    return "codex"


class CodexRunner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exe = _resolve_codex(cfg)
        print(f"[codex] using: {self.exe}")

    def run(self, prompt, schema, kind, label, logdir):
        schema_file = logdir / f"{label}.schema.json"
        out_file = logdir / f"{label}.out.json"
        schema_file.write_text(json.dumps(schema), encoding="utf-8")
        argv = [
            self.exe, "exec",
            "-c", f"service_tier={self.cfg['service_tier']}",
            "-c", f"model_reasoning_effort={self.cfg['reasoning_effort']}",
            "-m", self.cfg["model"],
            "--skip-git-repo-check", "-s", "read-only", "--ephemeral",
            "--color", "never",
            "--output-schema", str(schema_file),
            "-o", str(out_file),
            "-",  # prompt from stdin
        ]
        try:
            proc = _launch(argv, prompt, self.cfg["timeout_sec"])
        except subprocess.TimeoutExpired:
            raise RunnerError(f"codex timeout ({self.cfg['timeout_sec']}s)")
        (logdir / f"{label}.log.txt").write_text(
            f"$ {' '.join(argv)}\nexit={proc.returncode}\n\n--- stdout ---\n{proc.stdout}\n"
            f"\n--- stderr ---\n{proc.stderr}\n", encoding="utf-8")
        raw = ""
        if out_file.exists():
            raw = out_file.read_text(encoding="utf-8", errors="replace")
        if not raw.strip():
            raw = proc.stdout
        if proc.returncode != 0 and not raw.strip():
            raise RunnerError(f"codex exit {proc.returncode}: {proc.stderr[:300]}")
        return _extract_json(raw)


class ClaudeRunner:
    """claude CLI があれば使う。無ければ明示エラー(設計 §12: 後から差し替え可能)。"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.exe = shutil.which("claude")

    def run(self, prompt, schema, kind, label, logdir):
        if not self.exe:
            raise RunnerError("claude CLI が PATH に無い。config.engine を 'codex' にするか CLI を入れて下さい。")
        sys_p = ("出力は指定 JSON スキーマに厳密に従い、JSON のみを返す:\n"
                 + json.dumps(schema))
        argv = [self.exe, "-p", prompt, "--output-format", "json",
                "--append-system-prompt", sys_p]
        proc = _launch(argv, None, self.cfg["timeout_sec"])
        (logdir / f"{label}.log.txt").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
        data = json.loads(proc.stdout)
        return _extract_json(data.get("result", proc.stdout))


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
            return {
                "novelty":      {"assessment": "中程度（mock）", "confidence": "low"},
                "soundness":    {"assessment": "問題なし（mock）", "confidence": "medium"},
                "feasibility":  {"assessment": "実現可能（mock）", "confidence": "low"},
                "significance": {"assessment": "中（mock）", "confidence": "low"},
                "prior_art": [{"citation": "mock 2025", "source_tier": "preprint", "relation": "類似だが差分あり"}],
                "verdict": v,
                "kill_reason": "完全な先行事例あり（mock）" if v == "kill" else "",
                "notes": "mock verdict",
            }
        raise RunnerError(f"unknown kind {kind}")


def make_runner(cfg):
    return {"codex": CodexRunner, "claude": ClaudeRunner, "mock": MockRunner}[cfg["engine"]](cfg)


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
# 段階 (stages)
# ----------------------------------------------------------------------------
def planner(seed, constraints, cfg, run):
    """MVP の Planner は決定的: charter を固定するだけ(LLM 不使用)。"""
    charter = {
        "seed": seed,
        "constraints": constraints,
        "eval_axes": cfg["eval_axes"],
        "lenses": cfg["lenses"][: cfg["n_lenses"]],
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


def generate(runner, charter, cfg, run):
    """発散: レンズごとに独立生成(互いを見ない)。並列。"""
    tmpl = load_prompt("ideator")
    lenses = charter["lenses"]

    def one(i, lens):
        label = f"gen_{i:02d}__{lens}"
        prompt = render(tmpl, seed=charter["seed"], constraints=charter["constraints"],
                        lens=lens, lens_desc=LENS_DESC.get(lens, lens),
                        schema=json.dumps(HYPOTHESIS_SCHEMA, ensure_ascii=False))
        try:
            data = runner.run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", label, run["log"])
            data["id"] = f"rq-{i:02d}"
            data["_lens"] = lens
            data["provenance"] = provenance(run, cfg, "generate", label, lens=lens)
            return data
        except Exception as e:
            log(run, f"  [gen {lens}] FAILED: {e}")
            return None

    cands = _parallel(cfg, [(one, (i, lens)) for i, lens in enumerate(lenses)])
    cands = [c for c in cands if c]
    for c in cands:
        write_json(run, f"candidates/{c['id']}.json", c)
    log(run, f"  生成: {len(cands)}/{len(lenses)} 候補")
    return cands


def redteam(runner, cands, cfg, run):
    """cross red-team: 攻撃 -> 検証可能項目に変換。judge しない。"""
    tmpl = load_prompt("redteam")

    def one(c):
        label = f"review_{c['id']}"
        # blind: 著者(レンズ)情報は渡さない
        shown = {k: v for k, v in c.items()
                 if not k.startswith("_") and k not in ("id", "provenance")}
        prompt = render(tmpl, candidate=json.dumps(shown, ensure_ascii=False, indent=2),
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
        rv["provenance"] = provenance(run, cfg, "redteam", f"review_{c['id']}", target=c["id"])
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
        label = f"revise_{c['id']}"
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
            data["_review"] = c.get("_review", {"attacks": []})
            data["_verify_todo"] = c.get("_verify_todo", [])
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


def collect_prior_art(c, cfg, run):
    """Tier0 novelty の補助: arXiv API から候補文献を取り、artifact として残す。"""
    if not cfg.get("lit_search_enabled", True):
        return {"enabled": False, "query": "", "results": [], "error": ""}

    text = " ".join(str(c.get(k, "")) for k in
                    ("question", "hypothesis", "novelty_claim", "test_method"))
    terms = _search_terms(text, int(cfg.get("lit_search_max_terms", 6)))
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
                "source_tier": "authoritative_db",
                "relation": "arXiv search candidate; verifier must assess relation",
                "url": arxiv_id,
            })
    except Exception as e:
        data["error"] = str(e)
    write_json(run, f"evidence/{c['id']}.arxiv.json", data)
    return data


def verify(runner, cands, cfg, run):
    """Tier0 検証: 形(決定的) + 文献/soundness/feasibility(独立な検証呼び出し)。"""
    tmpl = load_prompt("verifier")
    required = HYPOTHESIS_SCHEMA["required"]

    def form_ok(c):
        missing = [k for k in required if k in ("assumptions", "unknowns")
                   and not c.get(k) or (k not in ("assumptions", "unknowns") and not str(c.get(k, "")).strip())]
        # cheapest_kill と falsification は特に重視
        for k in ("hypothesis", "falsification", "cheapest_kill", "test_method"):
            if not str(c.get(k, "")).strip():
                missing.append(k)
        return sorted(set(missing))

    def one(c):
        label = f"verify_{c['id']}"
        miss = form_ok(c)
        if miss:
            return c["id"], {"_form_fail": miss, "verdict": "kill",
                             "kill_reason": f"形不備(必須欠落): {', '.join(miss)}"}
        prior_art_hint = collect_prior_art(c, cfg, run)
        prompt = render(tmpl,
                        candidate=json.dumps({k: v for k, v in c.items() if not k.startswith("_")},
                                             ensure_ascii=False, indent=2),
                        todo="\n".join(c.get("_verify_todo", [])) or "(なし)",
                        prior_art_hint=json.dumps(prior_art_hint, ensure_ascii=False, indent=2),
                        schema=json.dumps(VERDICT_SCHEMA, ensure_ascii=False))
        try:
            verdict = runner.run(prompt, VERDICT_SCHEMA, "verdict", label, run["log"])
            verdict["provenance"] = provenance(run, cfg, "verify", label, target=c["id"])
            verdict["evidence_refs"] = [f"evidence/{c['id']}.arxiv.json"]
            return c["id"], verdict
        except Exception as e:
            log(run, f"  [verify {c['id']}] FAILED: {e}")
            return c["id"], {"verdict": "flag", "kill_reason": "",
                             "notes": f"検証エラー(要再実行): {e}",
                             "novelty": {"assessment": "未検証", "confidence": "low"},
                             "soundness": {"assessment": "未検証", "confidence": "low"},
                             "feasibility": {"assessment": "未検証", "confidence": "low"},
                             "significance": {"assessment": "未検証", "confidence": "low"},
                             "prior_art": []}

    verdicts = dict(_parallel(cfg, [(one, (c,)) for c in cands]))
    for c in cands:
        c["_verdict"] = verdicts.get(c["id"])
        write_json(run, f"verdicts/{c['id']}.json", c["_verdict"])
    return cands


def hard_gate(cands, run):
    """kill を落として捨て案台帳へ(消さない)。survivors = keep/flag。"""
    survivors, discarded = [], []
    for c in cands:
        v = c.get("_verdict", {})
        if v.get("verdict") == "kill":
            discarded.append((c, v.get("kill_reason", "(理由未記載)")))
        else:
            survivors.append(c)
    lines = ["# 捨て案台帳 (discarded)\n",
             "hard gate で落ちた候補。**消さずに理由付きで残す**(ARCHITECTURE §3.6)。\n"]
    for c, reason in discarded:
        lines += [f"## {c['id']}  (lens: {c.get('_lens','?')})",
                  f"- 仮説: {c.get('hypothesis','')}", f"- 棄却理由: **{reason}**", ""]
    if not discarded:
        lines.append("_(今回 hard gate で落ちた候補は無し)_\n")
    write_text(run, "discarded.md", "\n".join(lines))
    log(run, f"  hard gate: 生存 {len(survivors)} / 棄却 {len(discarded)}")
    return survivors


def arbiter(survivors, run):
    """idea段の Arbiter = 整理係。勝者を選ばず matrix を人間に出す。"""
    def cell(v, axis):
        a = v.get(axis, {})
        txt = str(a.get("assessment", "-")).replace("\n", " ").replace("|", "/")
        if len(txt) > 90:
            txt = txt[:90].rstrip() + "…"   # 全文は verdicts/*.json に残す
        return f"{txt} [{a.get('confidence','-')}]"

    rows = []
    for c in survivors:
        v = c.get("_verdict", {})
        rows.append({
            "id": c["id"], "lens": c.get("_lens", "?"),
            "verdict": v.get("verdict", "?"), "risk_type": c.get("risk_type", ""),
            "hypothesis": c.get("hypothesis", ""),
            "novelty": cell(v, "novelty"), "soundness": cell(v, "soundness"),
            "feasibility": cell(v, "feasibility"), "significance": cell(v, "significance"),
            "cheapest_kill": c.get("cheapest_kill", ""),
        })
    # 透明な並べ替え: keep を先, 次に high/medium confidence の数(単一スコアには潰さない)
    def score(r):
        order = {"keep": 0, "flag": 1}.get(r["verdict"], 2)
        strong = sum(("high" in r[a] or "medium" in r[a]) for a in
                     ("novelty", "soundness", "feasibility", "significance"))
        return (order, -strong)
    rows.sort(key=score)
    write_json(run, "decision_matrix.json", rows)

    md = ["# decision_matrix (人間が読む)\n",
          "**勝者は選んでいない。** AIは候補を出し客観検証しただけ。",
          "どの生存案に *実験予算* を割くかは人間が決める(ARCHITECTURE §3.7 / §5)。\n",
          "| id | lens | verdict | risk | novelty | soundness | feasibility | significance | cheapest_kill |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append("| {id} | {lens} | {verdict} | {risk_type} | {novelty} | {soundness} | "
                  "{feasibility} | {significance} | {cheapest_kill} |".format(
                      **{k: str(v).replace("|", "/").replace("\n", " ") for k, v in r.items()}))
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
    md = [f"# RUN REPORT — {run['id']}\n",
          f"- seed: **{charter['seed']}**",
          f"- constraints: {charter['constraints'] or '(なし)'}",
          f"- engine: {run['engine']} / model: {run['model']}",
          f"- 生成 {len(all_cands)} / 生存 {len(survivors)} / 棄却 {len(all_cands)-len(survivors)}",
          f"- created: {run['created']}\n",
          "## 読み方",
          "1. `decision_matrix.md` … 生存候補を評価軸ごとに(単一スコアに潰さず)一覧。",
          "2. `candidates/*.json` … 各 Research Hypothesis Contract。",
          "3. `reviews/*.json` … red-team の攻撃(検証項目へ変換済み)。",
          "4. `revised/*.json` … red-team 後の改訂版。原案は `candidates/` に残る。",
          "5. `evidence/*.json` … arXiv などから機械的に収集した検証補助データ。",
          "6. `verdicts/*.json` … Tier0 検証結果(novelty/soundness/feasibility + prior_art)。",
          "7. `discarded.md` … hard gate 落ち(理由付き)。 `unresolved.md` … 未解決・未追跡変種。",
          "",
          "## 次の一手(人間)",
          "- 生存案のうち `cheapest_kill` が安いものから Tier1(toy計算/既存データ再解析)に回す。",
          "- prior_art の source_tier が低い novelty 判定は、権威DBで裏取りする。",
          "- flag(通説違反など)は消さず、面白い線として別途検討。",
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
    "engine": "codex", "model": "gpt-5.5", "reasoning_effort": "low",
    "service_tier": "flex", "concurrency": 4, "n_lenses": 4, "timeout_sec": 420,
    "lenses": ["analogy", "anomaly", "method-driven", "contrarian", "gap", "combination"],
    "eval_axes": ["novelty", "soundness", "feasibility", "significance"],
    "lit_search_enabled": True, "lit_search_max_terms": 6,
    "lit_search_max_results": 5, "lit_search_timeout_sec": 15,
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

    ap = argparse.ArgumentParser(description="IDEA-stage funnel MVP (ARCHITECTURE §11)")
    ap.add_argument("--seed", help="研究の種(問い or hunch)")
    ap.add_argument("--seed-file", help="種をファイルから読む")
    ap.add_argument("--constraints", default="", help="制約(使える装置/データ/計算資源 等)")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--engine", choices=["codex", "claude", "mock"], help="config を上書き")
    ap.add_argument("--n-lenses", type=int, help="使う発散レンズ数(config を上書き)")
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

    run = new_run(seed, cfg)
    runner = make_runner(cfg)
    print(f"\n=== RUN {run['id']}  (engine={cfg['engine']}, model={cfg['model']}) ===")
    print(f"seed: {seed}\n")

    log(run, "[1/7] PLANNER  — charter 固定")
    charter = planner(seed, args.constraints, cfg, run)
    log(run, "[2/7] GENERATE — 発散(独立・並列)")
    cands = generate(runner, charter, cfg, run)
    if not cands:
        print("候補が0件。engine/認証/timeout を確認(--engine mock で配管だけ検証可)。")
        sys.exit(1)
    log(run, "[3/7] RED-TEAM — 攻撃 -> 検証項目へ変換")
    cands = redteam(runner, cands, cfg, run)
    log(run, "[4/7] REVISE   — 攻撃を受けて仮説を1回だけ改訂")
    cands = revise(runner, cands, cfg, run)
    log(run, "[5/7] VERIFY   — Tier0(形/文献/soundness/feasibility)")
    cands = verify(runner, cands, cfg, run)
    log(run, "[6/7] HARD GATE— kill を捨て案台帳へ")
    survivors = hard_gate(cands, run)
    log(run, "[7/7] ARBITER  — 整理(勝者は選ばない)")
    arbiter(survivors, run)
    write_unresolved(cands, run)
    report(charter, survivors, cands, run)

    write_text(run, "run.log", "\n".join(run["_logbuf"]))
    print(f"\n✅ 完了 → {run['dir']}")
    print(f"   まず読む: {run['dir'] / 'REPORT.md'}  /  {run['dir'] / 'decision_matrix.md'}")


if __name__ == "__main__":
    main()
