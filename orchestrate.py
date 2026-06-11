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
import importlib.util
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
TEMPLATES_DIR = ROOT / "templates"
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
        # 任意(revise の自己申告 lineage / Issue #35。orchestrator が _lineage へ移す)
        "changes":                  {"type": "array", "items": {"type": "string"}},
        "resolved_red_team_issues": {"type": "array", "items": {"type": "string"}},
    },
    ["id", "question", "hypothesis", "novelty_claim", "soundness", "falsification",
     "test_method", "feasibility", "significance_if_true", "risk_type",
     "cheapest_kill", "assumptions", "unknowns"],
)

# Proximity(Issue #34): クラスタ所属は決定的に確定済み。LLM はラベルのみ返す。
PROXIMITY_SCHEMA = _obj(
    {
        "clusters": {"type": "array", "items": _obj(
            {"cluster_id":        {"type": "string"},
             "theme":             {"type": "string"},
             "diversity_warning": {"type": "string"}},
            ["cluster_id", "theme", "diversity_warning"])},
        "underexplored_axes": {"type": "array", "items": {"type": "string"}},
    },
    ["clusters", "underexplored_axes"],
)

RESEARCH_PRIORITY_SCHEMA = _obj(
    {
        "recommendations": {"type": "array", "items": _obj(
            {
                "id":     {"type": "string"},
                "role":   {"type": "string"},
                "reason": {"type": "string"},
                "order":  {"type": "integer"},
            },
            ["id", "role", "reason", "order"])},
        "note": {"type": "string"},
    },
    ["recommendations", "note"],
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

# ---------------------------------------------------------------------------
# 人間向けラベル(Issue #47)。Markdown(decision_matrix / candidate_reports)だけで使い、
# JSON artifact は内部キーのまま(正本)。内部キーは traceability のため括弧で併記する。
# config の eval_axes_label で上書き/追加できる(domain 軸用)。
# ---------------------------------------------------------------------------
AXIS_LABEL = {
    "novelty":            "新しさ(novelty)",
    "soundness":          "筋の良さ(soundness)",
    "feasibility":        "実行しやすさ(feasibility)",
    "significance":       "impact / インパクト(significance)",
    "mechanism_clarity":  "機構の明確さ(mechanism_clarity)",
    "validation_clarity": "検証の明確さ(validation_clarity)",
    "baseline_clarity":   "基準比較の明確さ(baseline_clarity)",
}
CONF_LABEL = {"low": "低", "medium": "中", "high": "高"}
TIER_LABEL = {"authoritative_db": "権威DB", "peer_reviewed": "査読済み",
              "preprint": "preprint", "web": "一般web"}
ATTACK_LABEL = {
    "hidden_assumption":  "隠れた前提",
    "contradicting_work": "矛盾しうる先行研究",
    "feasibility_hole":   "実現性の穴",
    "confound":           "交絡・別説明",
    "stronger_variant":   "より強い変種の提案",
}
# convert_to → (確認方法の表示名, 現 pipeline での実施状況)。
# 実施状況は決定的に言えることだけ書く(等級の捏造をしない / Issue #47 レビュー方針)。
CONVERT_LABEL = {
    "assumption":        ("前提として明示", "対応済み(assumptions に追記)"),
    "lit_check":         ("文献確認", "検索実施済み(評価は verifier 引用参照)"),
    "computation":       ("計算で確認", "提案のみ(toy 実行は未実装 / #17)"),
    "falsification_fix": ("検証法の改訂", "提案のみ(revise で反映の可能性 / 改訂版参照)"),
    "new_candidate":     ("別候補として追跡", "未追跡(unresolved.md 参照)"),
}


def axis_label(ax, cfg=None):
    return ((cfg or {}).get("eval_axes_label") or {}).get(ax) or AXIS_LABEL.get(ax, ax)


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
    """core 4軸を超えるドメイン軸の説明を verifier プロンプトに渡す文。
    追加軸が無ければ空文字(プロンプト側は『空なら無視』— "(なし)" を注入しない)。
    eval_axes_desc が null 等の malformed config にも耐える。"""
    descs = dict(AXIS_DESC)
    raw = cfg.get("eval_axes_desc") or {}
    if isinstance(raw, dict):
        descs.update(raw)
    extra = [a for a in axes if a not in DEFAULT_EVAL_AXES]
    return "\n".join(f"- **{a}** — {descs.get(a, a)}" for a in extra)


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


def _winpty_available():
    """対話セッション駆動(ADR-001)の前提: Windows + pywinpty。"""
    return os.name == "nt" and importlib.util.find_spec("winpty") is not None


def engine_available(engine, cfg):
    """mock は常に可。codex/claude は Windows + pywinpty + 実行ファイル解決 が揃って初めて可
    (揃っていないのに _spawn まで進んで生 ImportError で落ちるのを防ぐ)。"""
    if engine == "mock":
        return True
    if not _winpty_available():
        return False
    return _resolve_engine_exe(engine, cfg) is not None


def _engine_argv(engine, exe, seed, cfg):
    """対話(非 headless)起動の argv。低フリクション(無確認・workspace書込)で seed prompt を渡す。"""
    if engine == "codex":
        # ネスト環境で codex の windows sandbox spawn が失敗するため、サンドボックスを bypass
        # (worker は queue/ 内の read/write のみ。orchestrator は信頼コード)
        # service_tier は config 駆動(#51): flex はプリエンプトされ得るため、quota 逼迫時に
        # config.json で default へ切替えられるようにする(従来は flex がハードコードだった)
        # `or` フォールバック: config に "service_tier": null / "" と書かれても
        # service_tier=None のような壊れた引数を codex に渡さない(PR #54 の Copilot 指摘)
        return [exe, "-c", f"service_tier={cfg.get('service_tier') or 'flex'}",
                "-c", f"model_reasoning_effort={cfg.get('reasoning_effort') or 'low'}",
                "--dangerously-bypass-approvals-and-sandbox", seed]
    if engine == "claude":
        # skip-permissions: BypassPermissions 承認は初回一度きり(~/.claude.json に記録)。
        # 承認済みなら以降プロンプト無し。rename 等シェルも write-execute プレーン内は無確認。
        return [exe, "--dangerously-skip-permissions", seed]
    raise ValueError(f"unknown engine {engine}")


class EventLog:
    """run の観測ログ(Issue #46)。**可視化・診断のみ** — LLM の生出力を採用判断に使わない。
    events.jsonl = 状態遷移の履歴(遷移時のみ追記) / status.json = engine ごとの現在状態(上書き)。
    dual(2エンジン並走)でも壊れないようプロセス内 lock で直列化。書き込み失敗は run を止めない。"""
    def __init__(self, rundir):
        self.dir = Path(rundir)
        self._lock = threading.Lock()
        self._status = {}

    def event(self, engine, label, event, **kw):
        rec = {"ts": _now(), "engine": engine, "label": label, "event": event, **kw}
        with self._lock:
            with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def status(self, engine, **fields):
        with self._lock:
            st = self._status.setdefault(engine, {})
            st.update(fields)
            st["updated_at"] = _now()
            tmp = self.dir / "status.json.tmp"
            tmp.write_text(json.dumps(self._status, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.dir / "status.json")


EVENTS = None   # main() が run 開始時に EventLog を差す(無ければ全て no-op)


def _emit(engine, label, event, **kw):
    if EVENTS is not None:
        try:
            EVENTS.event(engine, label, event, **kw)
        except Exception:
            pass


def _wd_status(engine, **kw):
    if EVENTS is not None:
        try:
            EVENTS.status(engine, **kw)
        except Exception:
            pass


# 承認/ログイン待ちらしき文言(ヒューリスティック)。state ではなく hint として併記する
# (TUI 文字列は version 依存で脆い — ADR-001 の「TUI 解析は最小化」に従い誤検知を state に昇格させない)
_HINT_RE = re.compile(r"(?i)yes,? i accept|do you trust|grant access|permission|approve|log ?in|sign ?in"
                      r"|承認|許可し|ログイン")


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
        self.buf_max = int(cfg.get("session_buffer_max_chunks", 256))  # TUI 出力の保持上限(チャンク数)
        self.tmp_stale = int(cfg.get("watchdog_tmp_stale_sec", 60))    # .json.tmp がこの秒数 rename されなければ stale(#46)
        self.proc = None
        self._buf = []
        self.bytes_total = 0        # watchdog(#46): セッション出力の累計バイト
        self.last_output_ts = None  # watchdog(#46): 最後に出力が増えた時刻
        self._blk = threading.Lock()
        self._lock = threading.Lock()   # 1セッション=直列。job 注入が交錯しないように

    def _directive(self, label):
        # .tmp に書いてから rename(atomic)。orchestrator の途中読みを原理的に防ぐ。
        # rename はシェルだが claude=skip-permissions / codex=bypass で無確認に通る。
        return (f"queue/{self.engine}/inbox/{label}.json を読み、その prompt に従い schema に厳密準拠した "
                f"JSON だけを、まず queue/{self.engine}/reports/{label}.json.tmp に書いてから "
                f"queue/{self.engine}/reports/{label}.json へ rename してください。説明やコードフェンスは書かない。")

    def _spawn(self, seed):
        try:
            from winpty import PtyProcess  # 遅延 import(mock/offline は pywinpty 不要)
        except ImportError as e:
            raise RunnerError(
                f"pywinpty が import できない({e})。Windows で `pip install -r requirements.txt` を実行"
            ) from e
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
                        self.bytes_total += len(d)
                        self.last_output_ts = time.time()
                        if len(self._buf) > self.buf_max:      # 無制限に溜めない(_dump は tail しか使わない)
                            del self._buf[: len(self._buf) - self.buf_max]
                else:
                    time.sleep(0.05)
        threading.Thread(target=_readloop, daemon=True).start()
        _emit(self.engine, "-", "session_spawn")
        _wd_status(self.engine, state="starting", label="-", proc_alive=True)
        time.sleep(self.warmup)   # 起動待ち

    def _cleanup(self, files, logdir, label, save_invalid=False):
        """queue/ にファイルを溜めない(成功時は即、失敗時も job の残骸を掃除)。
        save_invalid: parse できなかった report を削除前に log へ退避。"""
        report_f = self.qout / f"{label}.json"
        if save_invalid and report_f.exists():
            try:
                (logdir / f"{label}.invalid-report.txt").write_text(
                    report_f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            except OSError:
                pass
        for p in files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def run(self, prompt, schema, kind, label, logdir):
        with self._lock:
            inbox_f = self.qin / f"{label}.json"
            report_f = self.qout / f"{label}.json"
            report_tmp = self.qout / f"{label}.json.tmp"   # worker が rename 前に書く側
            # 同一 label を扱う別プロセスと衝突しないよう pid/tid で一意化
            tmp_f = self.qin / f".{label}.{os.getpid()}.{threading.get_ident()}.tmp"
            if report_f.exists():
                report_f.unlink()
            tmp_f.write_text(json.dumps(
                {"label": label, "kind": kind, "schema": schema, "prompt": prompt, "created": _now()},
                ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_f.replace(inbox_f)
            directive = self._directive(label)
            job_t0 = time.time()   # job 単位の出力基準。前 job の出力時刻を引きずって active 誤判定しない
            if self.proc is None:
                self._spawn(directive)                  # 最初の job は seed prompt として起動時に渡す
            else:
                self.proc.write(directive)              # 注入: テキストと Enter を別 write に分ける(ADR-001)
                time.sleep(self.enter_delay)
                self.proc.write("\r")
            _emit(self.engine, label, "directive_sent", kind=kind)
            started = _now()
            # 注入直後の現在状態も status に一度反映(watcher が job 開始を見られるように)
            _wd_status(self.engine, state="directive_sent", label=label, kind=kind,
                       started_at=started, bytes_total=self.bytes_total,
                       last_output_age_sec=None, report_tmp_age_sec=None,
                       proc_alive=(self.proc.isalive() if self.proc is not None else False), hint="")
            waited = 0
            tmp_seen_ts = None
            stale_emitted = invalid_emitted = False
            while waited < self.timeout:
                # --- watchdog(#46): 現在状態を status.json に反映(遷移は events.jsonl へ) ---
                with self._blk:
                    btot, lots = self.bytes_total, self.last_output_ts
                eff = lots if (lots and lots >= job_t0) else None   # この job 開始以降の出力のみ見る
                out_age = (time.time() - eff) if eff else None
                state = "active" if (out_age is not None and out_age <= 2 * self.poll) else "idle_waiting_report"
                if report_tmp.exists():
                    if tmp_seen_ts is None:
                        tmp_seen_ts = time.time()
                        _emit(self.engine, label, "report_tmp_seen")
                    if time.time() - tmp_seen_ts > self.tmp_stale and not stale_emitted:
                        _emit(self.engine, label, "report_tmp_stale",
                              age_sec=round(time.time() - tmp_seen_ts))
                        stale_emitted = True
                    if stale_emitted:
                        state = "report_tmp_stale"
                _wd_status(self.engine, state=state, label=label, kind=kind, started_at=started,
                           bytes_total=btot,
                           last_output_age_sec=(round(out_age) if out_age is not None else None),
                           report_tmp_age_sec=(round(time.time() - tmp_seen_ts) if tmp_seen_ts else None),
                           proc_alive=(self.proc.isalive() if self.proc is not None else False),
                           hint=self._hint())
                if report_f.exists():
                    raw = report_f.read_text(encoding="utf-8", errors="replace")
                    try:
                        data = _extract_json(raw)
                    except Exception as pe:
                        if not invalid_emitted:
                            _emit(self.engine, label, "invalid_report_retry", error=str(pe)[:120])
                            invalid_emitted = True
                        time.sleep(self.poll)      # 途中書き込みの可能性 → 次ポーリングで再読込
                        waited += self.poll
                        continue
                    (logdir / f"{label}.log.txt").write_text(
                        f"[{self.engine}-session] {label}\n--- report ---\n{raw}", encoding="utf-8")
                    self._cleanup((inbox_f, report_f, report_tmp), logdir, label)   # log に残したので queue は掃除
                    _emit(self.engine, label, "parsed_and_cleaned", bytes_total=btot)
                    _wd_status(self.engine, state="idle_done", label="-", kind="-", hint="")
                    return data
                if self.proc is not None and not self.proc.isalive():
                    self._dump(logdir, label, "session が終了(report 未生成)")
                    self._cleanup((inbox_f, report_f, report_tmp), logdir, label, save_invalid=True)
                    _emit(self.engine, label, "proc_dead")
                    _wd_status(self.engine, state="proc_dead", proc_alive=False)
                    raise RunnerError(f"{self.engine} session が終了(report 未生成) label={label}")
                time.sleep(self.poll)
                waited += self.poll
            # timeout: 直前まで出力が増えていたか(脱線/長考)/ 完全に沈黙か(こけた)を区別(#46)。
            # この job 開始以降の出力だけを見る(前 job の出力で active 誤判定しない)
            last = self.last_output_ts
            tstate = ("timeout_active" if (last and last >= job_t0 and
                                           time.time() - last <= 2 * self.poll)
                      else "timeout_idle")
            self._dump(logdir, label, f"timeout {self.timeout}s ({tstate})")
            # parse できない report が残っていれば log/<label>.invalid-report.txt に退避してから掃除
            self._cleanup((inbox_f, report_f, report_tmp), logdir, label, save_invalid=True)
            _emit(self.engine, label, tstate, waited_sec=self.timeout,
                  invalid_report=invalid_emitted, hint=self._hint())
            _wd_status(self.engine, state=tstate, hint=self._hint())
            raise RunnerError(f"{self.engine} session timeout ({self.timeout}s/{tstate}) label={label}")

    def _hint(self):
        """承認/ログイン待ちらしき文言の検出(ヒューリスティック・参考情報)。"""
        with self._blk:
            tail = "".join(self._buf)[-2000:]
        m = _HINT_RE.search(tail)
        return f"approval/login らしき文言: …{m.group(0)}…" if m else ""

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
        delay = float((self.cfg or {}).get("mock_delay_sec") or 0)
        if delay > 0:   # deterministic steering/watchdog smoke 用。既定0なので通常の mock は高速のまま。
            time.sleep(delay)
        fail_kinds = set((self.cfg or {}).get("mock_fail_kinds") or [])
        fail_labels = [str(x) for x in ((self.cfg or {}).get("mock_fail_label_contains") or [])]
        if kind in fail_kinds or any(x in label for x in fail_labels):
            raise RunnerError(f"mock forced failure kind={kind} label={label}")
        body = prompt if len(prompt) <= 2000 else prompt[:400] + "\n...[snip]...\n" + prompt[-1500:]
        (logdir / f"{label}.log.txt").write_text(f"[mock] {label}\n--- prompt ---\n{body}", encoding="utf-8")
        _emit("mock", label, "directive_sent", kind=kind)   # watchdog smoke 用(#46)
        _wd_status("mock", state="active", label=label, kind=kind, proc_alive=True)
        # 候補番号(rq-NN / gen_NN)を優先して拾う。label 先頭の run id(タイムスタンプ)に
        # マッチすると idx==1 の kill 分岐が一生発火しない(mock の gate/priority 検証が死ぬ)
        m = re.search(r"(?:rq-|gen_)(\d+)", label) or re.search(r"(\d+)", label)
        idx = int(m.group(1)) if m else 0
        _emit("mock", label, "parsed_and_cleaned")
        _wd_status("mock", state="idle_done", label="-", kind="-", proc_alive=True)   # 完了後を active のまま残さない
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
        if kind == "proximity":
            return {"clusters": [{"cluster_id": "C1", "theme": "mock theme（残差学習）",
                                  "diversity_warning": "表現違いの同型（mock）"}],
                    "underexplored_axes": ["negative-control design（mock）",
                                           "toy-simulation-first（mock）"]}
        if kind == "research_priority":
            ids = []
            for x in re.findall(r"\brq-\d+\b", prompt):
                if x not in ids:
                    ids.append(x)
            if not ids:
                ids = ["rq-00"]
            return {"recommendations": [
                {"id": cid, "role": "最初に読む候補" if i == 0 else "育てる候補(推奨)",
                 "reason": "mock: 研究テーマとして整理しやすく、次の検証問いに落としやすい。",
                 "order": i + 1}
                for i, cid in enumerate(ids)
            ], "note": "mock research priority"}
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


REPORT_TEMPLATE_FILES = ("report_summary.md", "candidate_onepager.md")


def load_template(name):
    p = TEMPLATES_DIR / name
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        raise SystemExit(f"report template を読めません: {p} ({e})")


def validate_report_templates():
    for name in REPORT_TEMPLATE_FILES:
        load_template(name)


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
# Operator steering channel (Issue #53)
#   Human note -> runs/<id>/control/operator_notes.jsonl -> safe-boundary prompt block.
#   Notes are sticky: once accepted, they apply to every later matching stage until revoked.
# ----------------------------------------------------------------------------
STEERING_SCOPES = {"global", "generate", "proximity", "redteam", "revise", "verify", "next_round"}
STEERING_PRIORITIES = {"low", "normal", "urgent"}
STEERING_LOCK = threading.Lock()


def _runs_dir():
    return ROOT / "runs"


def _control_dir(run_or_dir):
    base = Path(run_or_dir["dir"] if isinstance(run_or_dir, dict) else run_or_dir)
    return base / "control"


def _jsonl(path):
    return _read_jsonl(Path(path))


def _append_jsonl_atomic(path, rec):
    """Append one JSONL record without read/replace races against concurrent readers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _replace_text_best_effort(path, text, retries=1):
    """Regenerate display/control files without letting Windows replace races kill the run."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries + 1):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{attempt}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return True
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < retries:
                time.sleep(0.05)
    return False


def _latest_run_dir():
    runs = sorted([p for p in _runs_dir().glob("*") if p.is_dir()], key=lambda p: p.name, reverse=True)
    return runs[0] if runs else None


def _active_file():
    return _runs_dir() / "ACTIVE.json"


def _read_json_file(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _run_dir_from_ref(ref):
    ref = _safe_component(ref, "run_ref") if ref not in ("active", "latest") else ref
    if ref == "latest":
        rd = _latest_run_dir()
        if not rd:
            raise ValueError("runs/ に run がありません")
        return rd
    if ref == "active":
        st = _read_json_file(_active_file(), {})
        rid = st.get("run_id")
        if st.get("state") != "active" or not rid:
            raise ValueError(
                "active な run がありません(最後の run は state=%s)。run id か latest を指定してください"
                % st.get("state", "?"))
        return _runs_dir() / _safe_component(rid, "run_id")
    return _runs_dir() / ref


def _control_state(run_or_dir):
    cd = _control_dir(run_or_dir)
    return _read_json_file(cd / "state.json", {}) or {}


def _steering_notes(run_or_dir):
    return _jsonl(_control_dir(run_or_dir) / "operator_notes.jsonl")


def _steering_applied(run_or_dir):
    return _jsonl(_control_dir(run_or_dir) / "applied_notes.jsonl")


def _note_is_active(note, revoked):
    return note.get("type", "note") == "note" and note.get("status") != "conflicted" and note.get("id") not in revoked


def _active_notes_for_stage(run, stage):
    notes = _steering_notes(run)
    revoked = {n.get("revokes") for n in notes if n.get("type") == "revoke" and n.get("status") != "conflicted"}
    active = []
    for n in notes:
        if not _note_is_active(n, revoked):
            continue
        scope = n.get("scope", "global")
        if scope == "global" or scope == stage:
            active.append(n)
    return active


def _applied_keys(run):
    return {(x.get("note_id"), x.get("label")) for x in _steering_applied(run)}


def _steering_counts(run_or_dir):
    notes = _steering_notes(run_or_dir)
    applied = _steering_applied(run_or_dir)
    revoked = {n.get("revokes") for n in notes if n.get("type") == "revoke" and n.get("status") != "conflicted"}
    note_rows = [n for n in notes if n.get("type", "note") == "note"]
    conflicted = [n for n in notes if n.get("status") == "conflicted"]
    applied_ids = {a.get("note_id") for a in applied}
    pending = [n for n in note_rows
               if n.get("status") != "conflicted" and n.get("id") not in revoked
               and n.get("scope") not in ("next_round",) and n.get("id") not in applied_ids]
    next_round = [n for n in note_rows
                  if n.get("status") != "conflicted" and n.get("id") not in revoked
                  and n.get("scope") == "next_round"]
    return {"received": len(note_rows), "applied_events": len(applied), "applied_notes": len(applied_ids),
            "pending": len(pending), "next_round": len(next_round),
            "conflicted": len(conflicted), "revoked": len(revoked)}


def init_operator_control(run):
    cd = _control_dir(run)
    cd.mkdir(parents=True, exist_ok=True)
    readme = cd / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Operator Control\n\n"
            "Human steering notes for this run. Notes guide attention only; they are not evidence,\n"
            "do not raise evidence level, and do not override the locked charter or verifier output.\n\n"
            "- `operator_notes.jsonl`: accepted/conflicted/revoked human notes\n"
            "- `applied_notes.jsonl`: sticky note application events(note_id x LLM job label)\n"
            "- `state.json`: current control summary\n"
            "- `operator_control.md`: human-readable trace\n",
            encoding="utf-8")
    update_operator_state(run, "active")
    write_operator_control(run)


def update_operator_state(run, state, **extra):
    cd = _control_dir(run)
    cd.mkdir(parents=True, exist_ok=True)
    prev = _control_state(run)
    data = {**prev, "run_id": run["id"], "state": state, "updated_at": _now(),
            "started_at": prev.get("started_at") or run.get("created")}
    if state in ("completed", "failed"):
        data["completed_at"] = _now()
    data.update(extra)
    data["counts"] = _steering_counts(run)
    _replace_text_best_effort(cd / "state.json", json.dumps(data, ensure_ascii=False, indent=2))
    af = _active_file()
    active = {"run_id": run["id"], "state": state, "updated_at": _now(), "path": str(run["dir"])}
    _replace_text_best_effort(af, json.dumps(active, ensure_ascii=False, indent=2))


def _operator_note_text(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = _SECRET_RE.sub("[REDACTED]", text)
    return text[:1200]


def _next_note_id(rd):
    # Best-effort human-facing id. Concurrent steer processes can collide, but JSONL append remains valid.
    nums = []
    for n in _steering_notes(rd):
        m = re.match(r"op-(\d+)$", str(n.get("id", "")))
        if m:
            nums.append(int(m.group(1)))
    return f"op-{(max(nums) + 1) if nums else 1:03d}"


def _append_operator_note(rd, note, update_trace=True):
    with STEERING_LOCK:
        _append_jsonl_atomic(_control_dir(rd) / "operator_notes.jsonl", note)
        if update_trace:
            write_operator_control(rd)


def cmd_steer(argv):
    ap = argparse.ArgumentParser(prog="orchestrate.py steer")
    ap.add_argument("run_ref", help="run id | active | latest")
    ap.add_argument("note", nargs="?", help="human steering note")
    ap.add_argument("--scope", choices=sorted(STEERING_SCOPES), default="global")
    ap.add_argument("--priority", choices=sorted(STEERING_PRIORITIES), default="normal")
    ap.add_argument("--author", default="human")
    ap.add_argument("--revoke", help="note id to revoke")
    a = ap.parse_args(argv)
    try:
        rd = _run_dir_from_ref(a.run_ref)
    except ValueError as e:
        print(e)
        sys.exit(1)
    if not rd.exists():
        print(f"run が見つかりません: {rd}")
        sys.exit(1)

    state = _control_state(rd).get("state", "")
    ended = state in ("completed", "failed") or (state != "active" and (rd / "REPORT.md").exists())
    if a.revoke:
        rec = {"id": _next_note_id(rd), "type": "revoke", "created": _now(), "author": a.author,
               "revokes": a.revoke, "status": "conflicted" if ended else "active"}
        if ended:
            rec["conflict_reason"] = "run is already completed; revoke was recorded but will not affect prompts"
        _append_operator_note(rd, rec)
        print(f"steer revoke 記録 → {rd / 'control' / 'operator_notes.jsonl'}")
        return

    if not a.note or not a.note.strip():
        ap.error("note が必要です")
    status = "conflicted" if ended else "active"
    rec = {"id": _next_note_id(rd), "type": "note", "created": _now(), "scope": a.scope,
           "priority": a.priority, "author": a.author, "note": _operator_note_text(a.note),
           "status": status}
    if ended:
        rec["conflict_reason"] = "run is already completed; note was not applied"
    _append_operator_note(rd, rec)
    print(f"steer note 記録: {rec['id']} ({status}) → {rd / 'control' / 'operator_notes.jsonl'}")


def _steering_block(notes, stage):
    lines = ["",
             "## Operator steering notes",
             "These are human steering notes. They may guide attention and next-round allocation.",
             "They are not evidence. Do not treat them as literature, experiment, verifier result, or truth.",
             "They cannot raise evidence level, kill a hypothesis, or override the locked charter.",
             "If a note appears to conflict with the locked charter, preserve the charter and mention the conflict.",
             f"Current stage: {stage}",
             ""]
    for n in notes:
        lines.append(f"- [{n.get('id')}][scope={n.get('scope')}][priority={n.get('priority')}] {n.get('note')}")
    return "\n".join(lines) + "\n"


def apply_steering(prompt, run, stage, label, kind, engine="?"):
    """Append sticky human notes at safe LLM job boundaries(stage side, not Runner side)."""
    notes = _active_notes_for_stage(run, stage)
    if not notes:
        return prompt
    applied_now = []
    with STEERING_LOCK:
        done = _applied_keys(run)
        for n in notes:
            key = (n.get("id"), label)
            if key in done:
                continue
            rec = {"ts": _now(), "note_id": n.get("id"), "scope": n.get("scope"),
                   "stage": stage, "label": label, "kind": kind, "engine": engine}
            _append_jsonl_atomic(_control_dir(run) / "applied_notes.jsonl", rec)
            applied_now.append(rec)
        if applied_now:
            write_operator_control(run)
    return prompt + _steering_block(notes, stage)


def write_operator_control(run_or_dir):
    rd = Path(run_or_dir["dir"] if isinstance(run_or_dir, dict) else run_or_dir)
    cd = _control_dir(rd)
    cd.mkdir(parents=True, exist_ok=True)
    notes = _steering_notes(rd)
    applied = _steering_applied(rd)
    revoked = {n.get("revokes") for n in notes if n.get("type") == "revoke" and n.get("status") != "conflicted"}
    applied_by_note = {}
    for a in applied:
        applied_by_note.setdefault(a.get("note_id"), []).append(a)
    counts = _steering_counts(rd)
    md = ["# Operator Control Trace\n",
          "| item | count |", "|---|---|",
          f"| received notes | {counts['received']} |",
          f"| applied note/job events | {counts['applied_events']} |",
          f"| pending notes | {counts['pending']} |",
          f"| next_round notes | {counts['next_round']} |",
          f"| conflicted notes | {counts['conflicted']} |",
          f"| revoked notes | {counts['revoked']} |",
          "",
          "## Applied",
          "| note_id | scope | applied_to | engine | summary |", "|---|---|---|---|---|"]
    any_applied = False
    for n in notes:
        if not _note_is_active(n, revoked):
            continue
        for a in applied_by_note.get(n.get("id"), []):
            md.append("| " + " | ".join([
                n.get("id", ""), n.get("scope", ""), a.get("label", ""), a.get("engine", ""),
                str(n.get("note", "")).replace("|", "/")[:160]]) + " |")
            any_applied = True
    if not any_applied:
        md.append("| - | - | - | - | - |")

    md += ["", "## Pending",
           "| note_id | scope | reason | summary |", "|---|---|---|---|"]
    any_pending = False
    for n in notes:
        if not _note_is_active(n, revoked):
            continue
        if n.get("scope") == "next_round":
            md.append(f"| {n.get('id')} | next_round | 次 run / memory_suggestions へ反映候補 | {str(n.get('note','')).replace('|','/')[:160]} |")
            any_pending = True
        elif not applied_by_note.get(n.get("id")):
            md.append(f"| {n.get('id')} | {n.get('scope')} | scope に合う後続 job 待ち | {str(n.get('note','')).replace('|','/')[:160]} |")
            any_pending = True
    if not any_pending:
        md.append("| - | - | - | - |")

    md += ["", "## Conflicted / Revoked",
           "| note_id | type | reason | summary |", "|---|---|---|---|"]
    any_conf = False
    for n in notes:
        if n.get("status") == "conflicted" or n.get("type") == "revoke" or n.get("id") in revoked:
            reason = n.get("conflict_reason") or ("revoked" if n.get("id") in revoked else "")
            md.append(f"| {n.get('id')} | {n.get('type','note')} | {reason} | {str(n.get('note') or n.get('revokes') or '').replace('|','/')[:160]} |")
            any_conf = True
    if not any_conf:
        md.append("| - | - | - | - |")
    _replace_text_best_effort(cd / "operator_control.md", "\n".join(md) + "\n")

    st = _control_state(rd)
    if not st:
        st = {"run_id": rd.name, "state": "completed" if (rd / "REPORT.md").exists() else "unknown"}
    st.update({"updated_at": _now(), "counts": counts})
    _replace_text_best_effort(cd / "state.json", json.dumps(st, ensure_ascii=False, indent=2))
    return counts


# ----------------------------------------------------------------------------
# Fallback / degradation accounting (Issue #55)
# ----------------------------------------------------------------------------
FALLBACK_LOCK = threading.Lock()


def _short_error(e, n=300):
    return _SECRET_RE.sub("[REDACTED]", str(e))[:n]


def record_fallback(run, stage, label, candidate_id="", fallback_type="", effect="", reason="", engine=""):
    """Record a deterministic degradation event; do not infer from human log text."""
    rec = {
        "ts": _now(),
        "stage": stage,
        "label": label,
        "candidate_id": candidate_id,
        "fallback_type": fallback_type,
        "effect": effect,
        "reason": _SECRET_RE.sub("[REDACTED]", str(reason))[:500],
        "engine": engine,
    }
    with FALLBACK_LOCK:
        run.setdefault("_fallbacks", []).append(rec)
    return rec


def attach_candidate_fallback(c, rec):
    item = {k: rec[k] for k in ("stage", "candidate_id", "fallback_type", "effect", "engine")
            if rec.get(k)}
    c.setdefault("_fallbacks", []).append(item)


def fallback_records(run):
    return list(run.get("_fallbacks") or [])


def write_fallbacks(run):
    recs = fallback_records(run)
    by_stage = {}
    affected = sorted({r.get("candidate_id") for r in recs if r.get("candidate_id")})
    for r in recs:
        by_stage[r.get("stage", "?")] = by_stage.get(r.get("stage", "?"), 0) + 1
    write_json(run, "fallbacks.json", {
        "count": len(recs),
        "by_stage": by_stage,
        "affected_candidates": affected,
        "records": recs,
    })


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
        "rounds": 1,                              # 実行される round 数(>1 の実行は #38)
        "budget": cfg.get("_budget") or {"profile": "(none)"},   # 要求プロファイルの記録(#37)
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
        prompt = apply_steering(prompt, run, "generate", label, "hypothesis", engine=eng)
        used = eng
        used_label = label
        fallback_rec = None
        try:
            data = get_runner(eng).run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", label, run["log"])
        except Exception as e:
            if eng != fallback:   # 失敗したら別の live engine にフォールバック
                log(run, f"  [gen {lens}] {eng} 失敗({e}) → {fallback} で再試行")
                try:
                    used_label = f"{label}__fallback_{fallback}"
                    data = get_runner(fallback).run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", used_label, run["log"])
                    used = f"{fallback}(fallback<{eng})"
                    fallback_rec = record_fallback(
                        run, "generate", used_label, candidate_id=f"rq-{i:02d}",
                        fallback_type="engine_retry",
                        effect=f"{eng} failed; {fallback} generated the candidate",
                        reason=e, engine=fallback)
                except Exception as e2:
                    log(run, f"  [gen {lens}] {fallback} も失敗: {e2}")
                    record_fallback(
                        run, "generate", label, candidate_id=f"rq-{i:02d}",
                        fallback_type="candidate_dropped",
                        effect="candidate was not generated after primary and fallback failures",
                        reason=f"{eng}: {_short_error(e)} / {fallback}: {_short_error(e2)}",
                        engine=f"{eng}->{fallback}")
                    return None
            else:
                log(run, f"  [gen {lens}] FAILED: {e}")
                record_fallback(
                    run, "generate", label, candidate_id=f"rq-{i:02d}",
                    fallback_type="candidate_dropped",
                    effect="candidate was not generated",
                    reason=e, engine=eng)
                return None
        data["id"] = f"rq-{i:02d}"
        data["_lens"] = lens
        data["_engine"] = used
        if fallback_rec:
            attach_candidate_fallback(data, fallback_rec)
        # lineage(Issue #35)。アンダースコア内部キー = blind red-team プロンプトにリークしない
        data["_lineage"] = {"hypothesis_id": data["id"], "generation_round": 0, "parents": [],
                            "operator": "generate",
                            "changes": data.pop("changes", None) or [],
                            "resolved_red_team_issues": data.pop("resolved_red_team_issues", None) or []}
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


def proximity(runner, cands, charter, cfg, run):
    """within-run の重複検知・多様性確認(Issue #34 / Co-Scientist の Proximity 相当)。
    **注釈のみ** — これを理由に棄却しない。全メンバーが後段の red-team / verify を受ける。
    クラスタ所属は決定的(char-bigram Jaccard + union-find)。LLM はラベル付けのみ
    (theme / diversity_warning / underexplored_axes)で、失敗しても決定的注釈は残る。"""
    if not cfg.get("proximity_enabled", True) or not cands:
        return cands
    thr = float(cfg.get("proximity_sim_threshold", 0.45))
    ids = [c["id"] for c in cands]
    by_id = {c["id"]: c for c in cands}
    texts = {i: by_id[i].get("question", "") + by_id[i].get("hypothesis", "") for i in ids}

    parent = {i: i for i in ids}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pair_sim = {}
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            s = _similarity(texts[a], texts[b])
            pair_sim[f"{a}|{b}"] = round(s, 2)
            if s >= thr:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

    groups = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)

    def completeness(c):   # representative は契約の充足度で決定的に選ぶ(同点は id 順)
        return sum(1 for k in ("baseline", "success_metric", "failure_condition", "search_keywords")
                   if c.get(k))

    clusters = []
    for n, root in enumerate(sorted(groups), 1):
        members = sorted(groups[root])
        rep = sorted(members, key=lambda m: (-completeness(by_id[m]), m))[0]
        clusters.append({"cluster_id": f"C{n}", "theme": "", "members": members,
                         "representative": rep, "diversity_warning": ""})

    data = {"method": f"char-bigram jaccard >= {thr}(union-find)。LLM はラベルのみ・所属は変更しない",
            "clusters": clusters, "underexplored_axes": [], "pair_similarity": pair_sim}

    if cfg.get("proximity_llm_enabled", True):
        label = f"{run['id']}__proximity"
        shown = [{"id": c["id"], "lens": c.get("_lens", ""), "question": c.get("question", ""),
                  "hypothesis": c.get("hypothesis", "")} for c in cands]
        fixed = [{k: cl[k] for k in ("cluster_id", "members", "representative")} for cl in clusters]
        prompt = render(load_prompt("proximity"),
                        seed=charter["seed"], lenses=", ".join(charter["lenses"]),
                        candidates=json.dumps(shown, ensure_ascii=False, indent=2),
                        clusters=json.dumps(fixed, ensure_ascii=False, indent=2),
                        schema=json.dumps(PROXIMITY_SCHEMA, ensure_ascii=False))
        prompt = apply_steering(prompt, run, "proximity", label, "proximity",
                                engine=getattr(runner, "engine", cfg.get("engine", "?")))
        try:
            res = runner.run(prompt, PROXIMITY_SCHEMA, "proximity", label, run["log"])
            lbl = {x.get("cluster_id"): x for x in res.get("clusters", [])}
            for cl in clusters:
                if cl["cluster_id"] in lbl:
                    cl["theme"] = lbl[cl["cluster_id"]].get("theme", "")
                    cl["diversity_warning"] = lbl[cl["cluster_id"]].get("diversity_warning", "")
            data["underexplored_axes"] = res.get("underexplored_axes", [])
            data["provenance"] = provenance(run, cfg, "proximity", label)
        except Exception as e:
            log(run, f"  [proximity] ラベル付け失敗(決定的クラスタのみ残す): {e}")

    for cl in clusters:
        for m in cl["members"]:
            by_id[m]["_cluster_id"] = cl["cluster_id"]
            if len(cl["members"]) > 1 and m != cl["representative"]:
                by_id[m]["_cluster_rep"] = cl["representative"]
            lin = by_id[m].get("_lineage")
            if lin is not None:
                lin["cluster_id"] = cl["cluster_id"]
    write_json(run, "proximity.json", data)
    run["proximity"] = data
    nmulti = sum(1 for cl in clusters if len(cl["members"]) > 1)
    warn = sum(1 for cl in clusters if cl.get("diversity_warning"))
    log(run, f"  proximity: {len(cands)}候補 → {len(clusters)}クラスタ(複数員 {nmulti} / 同型警告 {warn})。注釈のみ・棄却しない")
    return cands


def redteam(runner, cands, cfg, run):
    """cross red-team: 攻撃 -> 検証可能項目に変換。judge しない。"""
    tmpl = load_prompt("redteam")
    checks = cfg.get("redteam_extra_checks") or []          # 無ければ空文字(プロンプト側は『空なら無視』)
    extra_checks = "\n".join(f"- {x}" for x in checks)

    def one(c):
        label = f"{run['id']}__review_{c['id']}"
        # blind: 著者(レンズ)情報は渡さない
        shown = {k: v for k, v in c.items()
                 if not k.startswith("_") and k not in ("id", "provenance")}
        prompt = render(tmpl, candidate=json.dumps(shown, ensure_ascii=False, indent=2),
                        extra_checks=extra_checks,
                        schema=json.dumps(REVIEW_SCHEMA, ensure_ascii=False))
        prompt = apply_steering(prompt, run, "redteam", label, "review",
                                engine=getattr(runner, "engine", cfg.get("engine", "?")))
        try:
            return c["id"], runner.run(prompt, REVIEW_SCHEMA, "review", label, run["log"])
        except Exception as e:
            log(run, f"  [review {c['id']}] FAILED: {e}")
            rec = record_fallback(
                run, "redteam", label, candidate_id=c["id"],
                fallback_type="empty_attacks",
                effect="red-team failed; attacks=[] so the candidate continued without attacks",
                reason=e, engine=getattr(runner, "engine", cfg.get("engine", "?")))
            return c["id"], {"attacks": [], "_fallback": rec}

    results = dict(_parallel(cfg, [(one, (c,)) for c in cands]))
    variants = []        # stronger_variant: 未追跡として未解決へ
    for c in cands:
        rv = results.get(c["id"], {"attacks": []})
        if rv.get("_fallback"):
            attach_candidate_fallback(c, rv["_fallback"])
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
        prompt = apply_steering(prompt, run, "revise", label, "hypothesis",
                                engine=getattr(runner, "engine", cfg.get("engine", "?")))
        try:
            data = runner.run(prompt, HYPOTHESIS_SCHEMA, "hypothesis", label, run["log"])
            data["id"] = c["id"]
            data["_lens"] = c.get("_lens", "?")
            data["_engine"] = c.get("_engine", "?")   # 生成 engine の印を改訂後にも引き継ぐ
            data["_review"] = c.get("_review", {"attacks": []})
            data["_verify_todo"] = c.get("_verify_todo", [])
            for k in ("_near_dup", "_cluster_id", "_cluster_rep"):
                if c.get(k):
                    data[k] = c[k]                    # 重複検知/クラスタの印を改訂後にも引き継ぐ
            # lineage(Issue #35)。changes/resolved は LLM の自己申告(検証済み事実ではない)
            data["_lineage"] = {"hypothesis_id": c["id"], "generation_round": 0,
                                "parents": [c["id"]], "operator": "revise", "self_reported": True,
                                "cluster_id": c.get("_cluster_id", ""),
                                "changes": data.pop("changes", None) or [],
                                "resolved_red_team_issues": data.pop("resolved_red_team_issues", None) or []}
            data["provenance"] = provenance(run, cfg, "revise", label,
                                            lens=c.get("_lens", "?"), revised_from=c["id"])
            return data
        except Exception as e:
            log(run, f"  [revise {c['id']}] FAILED: {e} / 原案を継続")
            rec = record_fallback(
                run, "revise", label, candidate_id=c["id"],
                fallback_type="original_continued",
                effect="revise failed; original candidate continued",
                reason=e, engine=getattr(runner, "engine", cfg.get("engine", "?")))
            attach_candidate_fallback(c, rec)
            c["_lineage"] = {**c.get("_lineage", {}), "operator": "revise_failed", "parents": [c["id"]]}
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
        prompt = apply_steering(prompt, run, "verify", label, "verdict",
                                engine=getattr(runner, "engine", cfg.get("engine", "?")))
        try:
            verdict = runner.run(prompt, vschema, "verdict", label, run["log"])
            verdict["provenance"] = provenance(run, cfg, "verify", label, target=c["id"])
            verdict["evidence_refs"] = evidence_refs(c)
            return c["id"], verdict
        except Exception as e:
            log(run, f"  [verify {c['id']}] FAILED: {e}")
            rec = record_fallback(
                run, "verify", label, candidate_id=c["id"],
                fallback_type="flag_unverified",
                effect="verify failed; verdict=flag with all axes 未検証",
                reason=e, engine=getattr(runner, "engine", cfg.get("engine", "?")))
            fb = {"verdict": "flag", "kill_reason": "",
                  "notes": f"検証エラー(要再実行): {_short_error(e)}", "prior_art": [],
                  "provenance": provenance(run, cfg, "verify", label, target=c["id"]),
                  "evidence_refs": evidence_refs(c),
                  "_fallback": rec}
            for ax in axes:
                fb[ax] = {"assessment": "未検証", "confidence": "low"}
            return c["id"], fb

    verdicts = dict(_parallel(cfg, [(one, (c,)) for c in cands]))
    for c in cands:
        c["_verdict"] = verdicts.get(c["id"])
        if (c.get("_verdict") or {}).get("_fallback"):
            attach_candidate_fallback(c, c["_verdict"]["_fallback"])
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


PRIORITY_ACTION = {   # 最も confidence が低い軸 → 追加予算の使い道(決定的マッピング)
    "novelty":      "deeper-lit-search",
    "soundness":    "soundness-derivation-check",
    "feasibility":  "toy-computation",
    "significance": "human-judgement",
}


def priority_for_next_round(survivors, axes, run):
    """次ラウンドの**追加**計算資源(深掘り red-team / 追加文献 / evolution 枠)の配分指針(Issue #37)。
    **採用判定ではない**(ranking ではなく allocation)。既存 artifact の信号だけから決定的に計算
    (LLM 不使用)。floor 保証: priority が低くても棄却されず、baseline 検証は全員が受け続ける。
    pairwise/debate 由来の信号は v1 では使わない(opt-in v2 / #36 の argument_trace 経由)。"""
    rows = []
    for c in survivors:
        v = c.get("_verdict", {}) or {}
        conf = {ax: (v.get(ax) or {}).get("confidence", "low") for ax in axes}
        breakdown = {   # 透明性: スカラに潰さず内訳を artifact に残す
            "uncertainty_low_axes":   2 * sum(1 for x in conf.values() if x == "low"),
            "uncertainty_med_axes":   1 * sum(1 for x in conf.values() if x == "medium"),
            "open_unknowns":          min(len(c.get("unknowns") or []), 5),
            "flag_attention":         2 if v.get("verdict") == "flag" else 0,
            "llm_kill_review":        1 if c.get("_llm_kill") else 0,      # 棄却理由の人間確認という追加作業
            "cluster_non_representative": -2 if c.get("_cluster_rep") else 0,  # 代表が同方向をカバー(#34)
            "past_duplicate":         -1 if c.get("_near_dup") else 0,
        }
        low_axes = [ax for ax in axes if conf[ax] == "low"]
        if c.get("_llm_kill"):
            action = "human-review-kill-reason"
        elif low_axes:
            action = PRIORITY_ACTION.get(low_axes[0], f"extra-verification({low_axes[0]})")
        elif v.get("verdict") == "flag":
            action = "extra-redteam"
        else:
            action = "baseline-only"
        rows.append({"id": c["id"],
                     "verdict": "kill?(LLM/要確認)" if c.get("_llm_kill") else v.get("verdict", "?"),
                     "priority_for_next_round": sum(breakdown.values()),
                     "breakdown": breakdown,
                     "recommended_next_action": action,
                     "status": "not_rejected"})     # priority は配分であり棄却ではない(floor)
    rows.sort(key=lambda r: (-r["priority_for_next_round"], r["id"]))
    write_json(run, "priority.json", {
        "_note": ("priority_for_next_round は『次ラウンドの追加計算資源の配分指針』であり、"
                  "採用/棄却の判定ではない。低 priority でも棄却されず、baseline 検証は全員が受け続ける(floor)。"
                  "既存 artifact からの決定的計算(LLM 不使用)。"),
        "floor": "all survivors keep full standing; priority allocates EXTRA compute only",
        "rows": rows})
    run["priority_rows"] = rows
    by_id = {r["id"]: r for r in rows}
    for c in survivors:
        c["_priority"] = by_id[c["id"]]["priority_for_next_round"]
        c["_next_action"] = by_id[c["id"]]["recommended_next_action"]
    log(run, f"  priority: 追加予算の配分指針を {len(rows)} 件に付与(採用判定ではない / floor 保証)")
    return rows


def candidate_fallbacks(c):
    return list(c.get("_fallbacks") or [])


def fallback_badge(c):
    fs = candidate_fallbacks(c)
    if not fs:
        return "-"
    stages = sorted({f.get("stage", "?") for f in fs})
    if "verify" in stages:
        return "未検証(fallback: " + ", ".join(stages) + ")"
    return "劣化あり(fallback: " + ", ".join(stages) + ")"


def candidate_status(c):
    v = c.get("_verdict", {}) or {}
    if v.get("_form_fail"):
        base = "客観棄却(形式不備)"
    elif c.get("_llm_kill"):
        base = "kill?(LLM/要人間確認)"
    else:
        base = {"keep": "継続候補", "flag": "要注目(flag)"}.get(v.get("verdict"), v.get("verdict", "?"))
    fb = fallback_badge(c)
    return base if fb == "-" else f"{base} / {fb}"


def _priority_rows(run):
    rows = run.get("priority_rows")
    if rows is not None:
        return rows
    p = run["dir"] / "priority.json"
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8")).get("rows", [])
        run["priority_rows"] = rows
        return rows
    except Exception:
        return []


def _priority_by_id(run):
    return {r.get("id"): r for r in _priority_rows(run)}


PRIORITY_BREAKDOWN_LABEL = {
    "uncertainty_low_axes": "根拠の弱い観点が多い",
    "uncertainty_med_axes": "中程度の不確実性が残る",
    "open_unknowns": "未解決の unknowns がある",
    "flag_attention": "要注目(flag)の確認が必要",
    "llm_kill_review": "LLM 棄却推奨の確認が必要",
    "cluster_non_representative": "同方向の代表候補がある",
    "past_duplicate": "過去 run と似ている",
}


def _priority_reason(row):
    bd = row.get("breakdown") or {}
    pos = [(k, v) for k, v in bd.items() if isinstance(v, (int, float)) and v > 0]
    pos.sort(key=lambda kv: (-kv[1], kv[0]))
    if not pos:
        return "baseline 継続"
    return " / ".join(PRIORITY_BREAKDOWN_LABEL.get(k, k) for k, _ in pos[:2])


def _one_line(c, *keys, limit=180):
    for k in keys:
        v = c.get(k)
        if isinstance(v, list):
            v = "; ".join(str(x) for x in v)
        if str(v or "").strip():
            return _md_cell(v, limit)
    return "-"


def _onepager_markdown(template, c):
    next_action = (f"{c.get('_priority','-')} → `{c.get('_next_action','-')}`"
                   if "_priority" in c else "-")
    return render(
        template,
        one_liner=_one_line(c, "question", "hypothesis", limit=220),
        why_interesting=_one_line(c, "significance_if_true", "novelty_claim", limit=260),
        first_kill=_one_line(c, "cheapest_kill", "falsification", limit=260),
        if_falsified=_one_line(c, "failure_condition", "falsification", limit=260),
        status=candidate_status(c),
        next_action=next_action,
    ).strip()


def arbiter(survivors, run, axes=None, cfg=None):
    """idea段の Arbiter = 整理係。勝者を選ばず matrix を人間に出す。
    評価軸は config の eval_axes 駆動(P0: 以前は4軸ハードコードだった)。"""
    axes = list(axes or DEFAULT_EVAL_AXES)
    cfg = cfg or {}
    show_fallback = any(candidate_fallbacks(c) for c in survivors)

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
            "id": c["id"], "lens": c.get("_lens", "?"), "cluster": c.get("_cluster_id", "-"),
            "engine": c.get("_engine", "?"),
            "verdict": status, "risk_type": c.get("risk_type", ""),
            "hypothesis": c.get("hypothesis", ""),
            "cheapest_kill": c.get("cheapest_kill", ""),
            # 配分指針(#37)。採用判定ではない(凡例参照)
            "next_round": (f'{c["_priority"]} → {c.get("_next_action", "-")}'
                           if "_priority" in c else "-"),
        }
        if show_fallback:
            row["fallback_warning"] = fallback_badge(c)
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

    labels = [axis_label(ax, cfg) for ax in axes]   # 人間向けラベル(内部キー併記 / #47)
    header_cols = ["id", "発想レンズ", "クラスタ", "engine", "状態(verdict)"]
    if show_fallback:
        header_cols.append("fallback警告")
    header_cols += ["リスク種別", *labels, "最短の棄却テスト(cheapest_kill)", "next_round(配分)"]
    header = "| " + " | ".join(header_cols) + " |"
    sep = "|" + "---|" * len(header_cols)
    md = ["# decision_matrix (人間が読む)\n",
          "**勝者は選んでいない。** AIは候補を出し客観検証しただけ。",
          "どの生存案に *実験予算* を割くかは人間が決める(ARCHITECTURE §3.7 / §5)。",
          "各候補の詳細・出典は `candidate_reports.md` を参照(#47)。\n",
          "状態(verdict)凡例: `keep` / `flag`(通説違反など要注目で残す) / "
          "`kill?(LLM/要確認)`=LLM は kill 推奨だが客観未確認 → 人間が棄却の妥当性を判断。\n",
          "next_round(配分)凡例: 次ラウンドで**追加**の検証予算をどこに使うかの決定的な指針"
          "(`priority.json` に内訳)。**採用判定ではない** — 低くても棄却されない(floor / #37)。\n",
          header, sep]
    for r in rows:
        cells = [r["id"], r["lens"], r["cluster"], r["engine"], r["verdict"]]
        if show_fallback:
            cells.append(r["fallback_warning"])
        cells.append(r["risk_type"])
        cells += [r[ax] for ax in axes]
        cells += [r["cheapest_kill"], r.get("next_round", "-")]
        md.append("| " + " | ".join(str(x).replace("|", "/").replace("\n", " ") for x in cells) + " |")
    md.append("\n## 各候補の仮説\n")
    for r in rows:
        md.append(f"- **{r['id']}** ({r['lens']}): {r['hypothesis']}")
    write_text(run, "decision_matrix.md", "\n".join(md))
    return rows


def build_hypothesis_graph(cands, run):
    """lineage を node/edge に集約(決定的・LLM 不使用 / Issue #35)。
    node は artifact 版: rq-NN.v0 = candidates/(原案)、rq-NN.v1 = revised/(改訂)。
    将来の evolution は .v2 / 新 id + parents で同じ規約に乗る。"""
    nodes = [{"id": "seed", "type": "seed"}]
    edges = []
    for c in cands:
        cid = c["id"]
        lin = c.get("_lineage", {})
        v = c.get("_verdict", {}) or {}
        status = ("discarded(form)" if v.get("_form_fail")
                  else "kill?(LLM/要確認)" if c.get("_llm_kill") else v.get("verdict", "?"))
        nodes.append({"id": f"{cid}.v0", "hypothesis_id": cid, "stage": "generate",
                      "operator": "generate", "generation_round": 0,
                      "lens": c.get("_lens", ""), "engine": c.get("_engine", ""),
                      "cluster_id": c.get("_cluster_id", ""),
                      "artifact": f"candidates/{cid}.json"})
        edges.append({"type": "generated_from", "from": f"{cid}.v0", "to": "seed"})
        edges.append({"type": "attacked_by", "from": f"{cid}.v0", "to": f"reviews/{cid}.json"})
        nodes.append({"id": f"{cid}.v1", "hypothesis_id": cid, "stage": "revise",
                      "operator": lin.get("operator", "revise"),
                      "generation_round": lin.get("generation_round", 0),
                      "status": status,
                      "changes": lin.get("changes", []),
                      "resolved_red_team_issues": lin.get("resolved_red_team_issues", []),
                      "self_reported": lin.get("self_reported", False),
                      "artifact": f"revised/{cid}.json"})
        edges.append({"type": "revised_from", "from": f"{cid}.v1", "to": f"{cid}.v0"})
        edges.append({"type": "verified_by", "from": f"{cid}.v1", "to": f"verdicts/{cid}.json"})
        if c.get("_near_dup"):
            edges.append({"type": "past_duplicate_of", "from": f"{cid}.v0",
                          "to": f"{c['_near_dup']['run']}/{c['_near_dup']['id']}",
                          "score": c["_near_dup"]["score"]})
    for cl in (run.get("proximity") or {}).get("clusters", []):
        ms = cl.get("members", [])
        for i, a in enumerate(ms):
            for b in ms[i + 1:]:
                edges.append({"type": "near_duplicate_of", "from": f"{a}.v0", "to": f"{b}.v0",
                              "cluster_id": cl["cluster_id"]})
    write_json(run, "hypothesis_graph.json", {"nodes": nodes, "edges": edges})

    md = ["# hypothesis graph (lineage / 人間が読む)\n",
          "各仮説が seed → 生成 → red-team 攻撃 → 改訂 → 検証 とどう育ったか。"
          "`resolved` は revise 時の **LLM 自己申告**(検証済み事実ではない)。\n"]
    for c in cands:
        cid = c["id"]
        lin = c.get("_lineage", {})
        v = c.get("_verdict", {}) or {}
        status = ("discarded(form)" if v.get("_form_fail")
                  else "kill?(LLM/要確認)" if c.get("_llm_kill") else v.get("verdict", "?"))
        n_att = len((c.get("_review") or {}).get("attacks", []))
        res = lin.get("resolved_red_team_issues", [])
        md.append(f"- **{cid}** [{c.get('_lens','?')} / {c.get('_engine','?')} / "
                  f"cluster {c.get('_cluster_id','-')}] "
                  f"seed → v0 → 攻撃{n_att}件 → {lin.get('operator','revise')} → v1 → **{status}**"
                  + (f" / resolved(自己申告): {', '.join(res[:4])}" if res else ""))
    write_text(run, "hypothesis_graph.md", "\n".join(md))


def _md_cell(x, n=400):
    """Markdown テーブルセル用に整形(パイプ・改行を潰し、長文は切って正本 JSON へ誘導)。"""
    t = str(x if x not in (None, "") else "-").replace("|", "/").replace("\n", " ")
    return (t[:n].rstrip() + "…") if len(t) > n else t


def _read_evidence_files(run, cid):
    out = {}
    for prov in ("arxiv", "inspire", "ntrs"):
        p = run["dir"] / "evidence" / f"{cid}.{prov}.json"
        if p.exists():
            try:
                out[prov] = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                out[prov] = {"enabled": True, "results": [], "error": f"unreadable: {e}"}
    return out


def _provider_status(prov, d):
    """collector artifact から決定的に言える実施状況だけを文にする。
    no_ascii_search_terms は collector の決定的状態(検索語が作れず未実施)であり、
    HTTP/例外の『エラー』とは区別して表示する(内部コードを露出させない)。"""
    if d.get("error") == "no_ascii_search_terms":
        return "未実施(検索語を抽出できず)"
    if d.get("error"):
        return f"エラー({_md_cell(d['error'], 60)})"
    if not d.get("enabled", False):
        return "スキップ(trigger 語なし)" if d.get("skipped") == "no_trigger_terms" else "無効(設定)"
    return f"実施済み・ヒット {len(d.get('results', []))} 件(query: {_md_cell(d.get('query',''), 60)})"


def _provider_from_prior_art(p):
    text = " ".join(str(p.get(k, "")) for k in ("citation", "relation", "url")).lower()
    if "ntrs" in text or "nasa" in text:
        return "ntrs"
    if "inspire" in text or "hep" in text:
        return "inspire"
    if "arxiv" in text or p.get("source_tier") == "preprint":
        return "arxiv"
    return "unknown"


def _prior_art_provider_counts(cands):
    counts = {"arxiv": 0, "inspire": 0, "ntrs": 0, "unknown": 0}
    for c in cands:
        for p in (c.get("_verdict") or {}).get("prior_art") or []:
            prov = _provider_from_prior_art(p)
            counts[prov] = counts.get(prov, 0) + 1
    return counts


def _verification_order_table(survivors, run):
    rows = list(_priority_rows(run))
    if not rows:
        if run.get("priority_rows") is not None or (run["dir"] / "priority.json").exists():
            return "（生存候補が無いため、次に検証する順番はありません）"
        return "（priority.json がありません）"
    by_id = {c["id"]: c for c in survivors}
    md = ["| 順 | 候補 | 現時点の扱い | 次にやること | 配分点 | 理由 |",
          "|---|---|---|---|---:|---|"]
    for i, row in enumerate(rows, 1):
        c = by_id.get(row.get("id"), {})
        md.append("| " + " | ".join([
            str(i),
            _md_cell(row.get("id")),
            _md_cell(candidate_status(c) if c else row.get("verdict", "-"), 80),
            _md_cell(row.get("recommended_next_action", "-"), 120),
            str(row.get("priority_for_next_round", "-")),
            _md_cell(_priority_reason(row), 160),
        ]) + " |")
    return "\n".join(md)


def _research_priority_section(run):
    data = run.get("research_priority")
    if not data:
        return "（無効、または未生成）"
    if data.get("disabled"):
        return "（設定で無効化: `grow_priority_enabled=false`）"
    if data.get("error"):
        return f"- 推奨生成に失敗: {_md_cell(data.get('error'), 180)}\n- `research_priority.json` を確認してください。"
    recs = sorted(data.get("recommendations") or [], key=lambda r: (r.get("order", 999), r.get("id", "")))
    if not recs:
        return "（推奨は空でした。人間が `decision_matrix.md` と `candidate_reports.md` から判断してください）"
    md = ["| 育てる順 | 候補 | 位置づけ | 理由 |",
          "|---:|---|---|---|"]
    for r in recs:
        md.append("| " + " | ".join([
            str(r.get("order", "-")),
            _md_cell(r.get("id")),
            _md_cell(r.get("role"), 140),
            _md_cell(r.get("reason"), 260),
        ]) + " |")
    note = data.get("note")
    if note:
        md += ["", f"- メモ: {_md_cell(note, 260)}"]
    md.append("- 正本: `research_priority.json`")
    return "\n".join(md)


def _first_actions(survivors, run):
    rows = _priority_rows(run)
    if not rows:
        return "- `priority.json` が無いため未作成。"
    by_id = {c["id"]: c for c in survivors}
    md = []
    for i, row in enumerate(rows[:3], 1):
        c = by_id.get(row.get("id"), {})
        md.append(f"{i}. **{row.get('id')}**: `{row.get('recommended_next_action', '-')}` — "
                  f"{_md_cell(c.get('cheapest_kill', '最短確認は candidate_reports.md を参照'), 180)}")
    return "\n".join(md) if md else "- 候補なし。"


def _search_quality_section(run, cands, cfg, charter):
    provider_names = {"arxiv": "arXiv", "inspire": "INSPIRE-HEP", "ntrs": "NASA NTRS"}
    adopted = _prior_art_provider_counts(cands)
    cfg_name = cfg.get("_config_path") or "(default)"
    if cfg_name not in ("(default)", ""):
        cfg_name = Path(cfg_name).name
    md = [
        f"- 使用 config: `{_md_cell(cfg_name, 120)}`",
        f"- 文献・出典検索: {'有効' if charter.get('lit_search_enabled', True) else '無効'}",
        "",
        "| provider | 実施状況 | 実クエリ | ヒット数 | verifier 採用数 |",
        "|---|---|---|---:|---:|",
    ]
    for prov in ("arxiv", "inspire", "ntrs"):
        docs = []
        for c in cands:
            docs.append(_read_evidence_files(run, c["id"]).get(prov, {}))
        existing = [d for d in docs if d]
        enabled = sum(1 for d in existing if d.get("enabled"))
        skipped = sum(1 for d in existing if d.get("skipped"))
        errors = [d.get("error") for d in existing if d.get("error")]
        hits = sum(len(d.get("results") or []) for d in existing)
        queries = []
        for d in existing:
            q = str(d.get("query") or "").strip()
            if q and q not in queries:
                queries.append(q)
        if not existing:
            status = "artifactなし"
        elif errors:
            status = f"エラー {len(errors)} 件 / 実施 {enabled}/{len(existing)}"
        elif enabled:
            status = f"実施 {enabled}/{len(existing)}"
        elif skipped:
            status = f"スキップ {skipped}/{len(existing)}"
        else:
            status = "無効"
        adopted_n = adopted.get(prov, 0) if enabled else 0
        qtxt = " / ".join(_md_cell(q, 80) for q in queries[:2]) or "-"
        if len(queries) > 2:
            qtxt += f" / 他 {len(queries) - 2} 件"
        md.append("| " + " | ".join([
            provider_names[prov],
            _md_cell(status, 80),
            qtxt,
            str(hits),
            str(adopted_n),
        ]) + " |")
    if adopted.get("unknown"):
        md.append(f"\n- provider を決定できない verifier 採用文献: {adopted['unknown']} 件"
                  "（source_tier は保持、provider 推定はしない）")
    return "\n".join(md)


def _warnings_section(run, all_cands):
    lines = []
    fbs = fallback_records(run)
    if fbs:
        by_stage = {}
        for f in fbs:
            by_stage[f.get("stage", "?")] = by_stage.get(f.get("stage", "?"), 0) + 1
        affected = sorted({f.get("candidate_id") for f in fbs if f.get("candidate_id")})
        lines += [
            f"- ⚠ fallbackによる静かな劣化: {len(fbs)} job が失敗し、fallback / 継続処理で完走しています。",
            f"- stage別: {', '.join(f'{k}:{v}' for k, v in sorted(by_stage.items()))}",
            f"- 影響候補: {', '.join(affected) if affected else '(候補生成前/候補なし)'}",
            "- 詳細: `fallbacks.json` / `events.jsonl`",
        ]
    else:
        lines.append("- fallback なし。")

    steer = _steering_counts(run)
    lines.append("- operator steering: "
                 f"received {steer['received']} / applied {steer['applied_events']} / "
                 f"pending {steer['pending']} / next_round {steer['next_round']} / "
                 f"conflicted {steer['conflicted']} / revoked {steer['revoked']}")
    lines.append("- steering は注意を向けるための情報で、evidence ではありません。")

    dups = [c for c in all_cands if c.get("_near_dup")]
    if dups:
        lines.append("- 過去 run との類似候補:")
        for c in dups:
            lines.append(f"  - {c['id']}: {c['_near_dup']['run']}/{c['_near_dup']['id']} "
                         f"(bigram {c['_near_dup']['score']})")
    prox = run.get("proximity") or {}
    clusters = prox.get("clusters") or []
    if clusters:
        lines.append("- within-run proximity:")
        for cl in clusters:
            members = ", ".join(cl.get("members") or [])
            rep = f" / 代表 {cl.get('representative')}" if cl.get("representative") else ""
            warn = f" / {cl.get('diversity_warning')}" if cl.get("diversity_warning") else ""
            lines.append(f"  - {cl.get('cluster_id')}: {cl.get('theme') or '-'} ({members}){rep}{warn}")
    if prox.get("underexplored_axes"):
        lines.append("- 未探索軸: " + ", ".join(prox["underexplored_axes"]))
    return "\n".join(lines)


def _links_section():
    return "\n".join([
        "- `candidate_reports.md`: 候補ごとの要約、詳細、反証・ツッコミ、出典",
        "- `decision_matrix.md`: 評価軸ごとの整理。勝者は選びません",
        "- `priority.json`: 次に検証する順番の正本",
        "- `research_priority.json`: 育てる順の LLM 推奨。採用判定ではありません",
        "- `evidence/*.json`: 文献・出典検索の生 artifact",
        "- `hypothesis_graph.{json,md}`: lineage",
        "- `memory_suggestions.md`: 記録候補の提案のみ",
    ])


def _memory_suggestions_section(run):
    n = run.get("memory_suggestions_n")
    if n is None:
        return "（未生成）"
    return f"- {n} 件 → `memory_suggestions.md`（提案のみ。コマンド実行が承認）"


BANNED_RESEARCH_PRIORITY = {
    "winner": "推奨候補",
    "best": "推奨",
    "truth_score": "評価メモ",
    "final_rank": "読む順",
    "本命": "最初に読む候補",
}


def _scrub_research_priority_text(text):
    out = str(text or "")
    for bad, good in BANNED_RESEARCH_PRIORITY.items():
        out = re.sub(re.escape(bad), good, out, flags=re.IGNORECASE)
    return _SECRET_RE.sub("[REDACTED]", out)


def _next_round_notes_text(run):
    notes = _steering_notes(run)
    revoked = {n.get("revokes") for n in notes if n.get("type") == "revoke" and n.get("status") != "conflicted"}
    active = [n for n in notes if _note_is_active(n, revoked) and n.get("scope") == "next_round"]
    if not active:
        return "(なし)"
    return "\n".join(f"- {n.get('id')}: {n.get('note')}" for n in active)


def research_priority(runner, charter, survivors, cfg, run):
    """Issue #61: 研究として育てる順を LLM 推奨・要確認として出す。
    decision_matrix / hard gate / evidence level には混ぜない。失敗しても run は完走する。"""
    if not cfg.get("grow_priority_enabled", True):
        data = {"recommendations": [], "note": "grow_priority_enabled=false", "disabled": True,
                "provenance": provenance(run, cfg, "research_priority", "disabled")}
        write_json(run, "research_priority.json", data)
        run["research_priority"] = data
        return data

    rows = _priority_by_id(run)
    payload = []
    for c in survivors:
        v = c.get("_verdict") or {}
        payload.append({
            "id": c["id"],
            "question": c.get("question", ""),
            "hypothesis": c.get("hypothesis", ""),
            "significance_if_true": c.get("significance_if_true", ""),
            "cheapest_kill": c.get("cheapest_kill", ""),
            "failure_condition": c.get("failure_condition", ""),
            "status": candidate_status(c),
            "priority_for_next_round": rows.get(c["id"], {}),
            "verdict_summary": {
                ax: {"assessment": (v.get(ax) or {}).get("assessment", ""),
                     "confidence": (v.get(ax) or {}).get("confidence", "")}
                for ax in charter["eval_axes"]
            },
            "prior_art_count": len(v.get("prior_art") or []),
        })
    label = f"{run['id']}__research_priority"
    tmpl = load_prompt("research_priority")
    prompt = render(tmpl,
                    candidates=json.dumps(payload, ensure_ascii=False, indent=2),
                    schema=json.dumps(RESEARCH_PRIORITY_SCHEMA, ensure_ascii=False))
    prompt = apply_steering(prompt, run, "next_round", label, "research_priority",
                            engine=getattr(runner, "engine", cfg.get("engine", "?")))
    try:
        raw = runner.run(prompt, RESEARCH_PRIORITY_SCHEMA, "research_priority", label, run["log"])
        allowed = {c["id"] for c in survivors}
        recs = []
        for i, rec in enumerate(raw.get("recommendations") or [], 1):
            cid = str(rec.get("id", ""))
            if cid not in allowed:
                continue
            try:
                order = int(rec.get("order", i))
            except Exception:
                order = i
            recs.append({"id": cid,
                         "role": _scrub_research_priority_text(rec.get("role")),
                         "reason": _scrub_research_priority_text(rec.get("reason")),
                         "order": order})
        recs.sort(key=lambda r: (r["order"], r["id"]))
        for i, rec in enumerate(recs, 1):
            rec["order"] = i
        data = {"recommendations": recs,
                "note": _scrub_research_priority_text(raw.get("note")),
                "label": "LLM 推奨・要確認。採用判定ではない。",
                "provenance": {**provenance(run, cfg, "research_priority", label),
                               "input_artifacts": ["candidates/*.json", "verdicts/*.json", "priority.json",
                                                   "control/operator_notes.jsonl"],
                               "next_round_notes": _next_round_notes_text(run)}}
    except Exception as e:
        log(run, f"  [research_priority] FAILED: {e}")
        data = {"recommendations": [],
                "note": "推奨生成に失敗。verdict 等から人間が判断してください。",
                "label": "LLM 推奨・要確認。採用判定ではない。",
                "error": _short_error(e),
                "provenance": {**provenance(run, cfg, "research_priority", label),
                               "input_artifacts": ["candidates/*.json", "verdicts/*.json", "priority.json",
                                                   "control/operator_notes.jsonl"],
                               "next_round_notes": _next_round_notes_text(run)}}
    write_json(run, "research_priority.json", data)
    run["research_priority"] = data
    log(run, f"  research_priority: {len(data.get('recommendations') or [])} 件を出力(LLM 推奨・要確認)")
    return data


def write_candidate_reports(charter, cands, run, cfg):
    """候補ごとの詳細・評価・出典を1つの Markdown に集約(Issue #47)。
    JSON artifact が正本で、これは人間向け view。内部キーは括弧で併記(用語対応表は末尾)。
    AI の指摘(red-team)は evidence と混ぜない。出典の無い主張は「未検証」と明示する。
    検証状況は現 pipeline が客観的に言えることだけ書く(等級の捏造をしない)。"""
    axes = charter["eval_axes"]
    lab = {ax: axis_label(ax, cfg) for ax in axes}
    show_fallback = any(candidate_fallbacks(c) for c in cands)
    onepager_template = load_template("candidate_onepager.md")

    def status_of(c):
        return candidate_status(c)

    def fallback_summary(c):
        fs = candidate_fallbacks(c)
        if not fs:
            return "-"
        return "; ".join(f"{x.get('stage','?')}:{x.get('fallback_type','fallback')}" for x in fs[:4])

    def fallback_rows(c):
        fs = candidate_fallbacks(c)
        if not fs:
            return []
        rows = ["", "### ⚠ fallback警告", "| stage | type | effect | label |", "|---|---|---|---|"]
        for x in fs:
            rows.append("| " + " | ".join([
                _md_cell(x.get("stage", "?"), 60),
                _md_cell(x.get("fallback_type", "fallback"), 80),
                _md_cell(x.get("effect", ""), 180),
                _md_cell(x.get("label", ""), 100),
            ]) + " |")
        rows.append("")
        return rows

    def has_fallback_stage(c, stage):
        return any(x.get("stage") == stage for x in candidate_fallbacks(c))

    def weakest(c):
        if has_fallback_stage(c, "verify"):
            return "未検証(fallback): verifier 失敗のため再実行が必要"
        v = c.get("_verdict", {}) or {}
        if v.get("_form_fail"):   # 客観棄却の主要情報なので一覧にも理由を出す
            return f"形式不備: {_md_cell(v.get('kill_reason',''), 80)}"
        if c.get("_llm_kill"):
            return f"LLM が棄却を推奨: {_md_cell(c.get('_llm_kill_reason',''), 80)}"
        low = [lab[ax] for ax in axes if (v.get(ax) or {}).get("confidence") == "low"]
        return ("根拠の弱い観点: " + "、".join(low)) if low else "-"

    prov_names = {"arxiv": "arXiv(preprint)", "inspire": "INSPIRE-HEP(権威DB)",
                  "ntrs": "NASA NTRS(権威DB)"}
    md = [f"# 候補別 詳細レポート — {run['id']}\n",
          "| 項目 | 内容 |", "|---|---|",
          f"| seed | {_md_cell(charter['seed'])} |",
          f"| constraints | {_md_cell(charter.get('constraints') or '(なし)')} |",
          *( [f"| domain | {_md_cell(charter['domain'])} |"] if charter.get("domain") else [] ),
          f"| engines | {_md_cell(', '.join(charter.get('engines', [])))} |",
          f"| 文献・出典検索 | {'有効' if charter.get('lit_search_enabled', True) else '無効(--no-lit-search)'} |",
          f"| 予算プロファイル | {_md_cell(charter.get('budget', {}).get('profile', '(none)'))} |",
          f"| 生成時刻 | {_md_cell(charter.get('created', ''))} |",
          "",
          "## このレポートの読み方",
          "- 正本は JSON artifact(`candidates/` `reviews/` `revised/` `verdicts/` `evidence/` "
          "`proximity.json` `hypothesis_graph.json` `priority.json`)。これはその人間向けビュー。",
          "- **AI の指摘(red-team)は根拠(evidence)ではない** — 確認すべき論点として読む。",
          "- 出典の無い主張は「未検証」のまま明示する(検証されたように見せない)。",
          "- 「次に深掘りする優先度」は追加検証の予算配分であり、**採用/棄却の判定ではない**(低くても棄却されない)。",
          "- 内部キー(英語)との対応は末尾の用語対応表を参照。",
          "",
          "## 候補一覧"]
    summary_header = ["ID", "問い", "engine", "発想レンズ", "クラスタ", "状態"]
    if show_fallback:
        summary_header.append("fallback警告")
    summary_header += ["次に深掘り(配分)", "最大の不確実点"]
    md += ["| " + " | ".join(summary_header) + " |", "|" + "---|" * len(summary_header)]
    for c in cands:
        cells = [
            c["id"], _md_cell(c.get("question", ""), 80), c.get("_engine", "?"),
            c.get("_lens", "?"), c.get("_cluster_id", "-"), status_of(c)]
        if show_fallback:
            cells.append(fallback_summary(c))
        cells += [
            (f'{c["_priority"]} → {c.get("_next_action","-")}' if "_priority" in c else "-"),
            weakest(c)]
        md.append("| " + " | ".join(cells) + " |")
    md.append("")

    for c in cands:
        cid = c["id"]
        v = c.get("_verdict", {}) or {}
        lin = c.get("_lineage", {}) or {}
        md += [f"---\n\n## {cid}: {_md_cell(c.get('question',''), 120)}\n",
               f"**状態: {status_of(c)}**\n",
               _onepager_markdown(onepager_template, c),
               "",
               *fallback_rows(c),
               "### 1. 案の内容",
               "| 項目 | 内容 |", "|---|---|",
               f"| 問い(question) | {_md_cell(c.get('question'))} |",
               f"| 仮説(hypothesis) | {_md_cell(c.get('hypothesis'))} |",
               f"| 新しさの主張(novelty_claim) | {_md_cell(c.get('novelty_claim'))} |",
               f"| 理屈の説明(soundness) | {_md_cell(c.get('soundness'))} |",
               f"| 検証方法(test_method) | {_md_cell(c.get('test_method'))} |",
               f"| 反証条件(falsification) | {_md_cell(c.get('falsification'))} |",
               f"| 比較基準(baseline) | {_md_cell(c.get('baseline'))} |",
               f"| 成功指標(success_metric) | {_md_cell(c.get('success_metric'))} |",
               f"| 失敗条件(failure_condition) | {_md_cell(c.get('failure_condition'))} |",
               f"| 最短でダメと分かる確認(cheapest_kill) | {_md_cell(c.get('cheapest_kill'))} |",
               f"| 前提(assumptions) | {_md_cell('; '.join(c.get('assumptions') or []))} |",
               f"| 未知の点(unknowns) | {_md_cell('; '.join(c.get('unknowns') or []))} |",
               "",
               "### 2. 系譜・重複",
               f"- 生成: レンズ `{c.get('_lens','?')}` / engine `{c.get('_engine','?')}` / "
               f"操作 `{lin.get('operator','?')}`(round {lin.get('generation_round', 0)})",
               f"- クラスタ: `{c.get('_cluster_id','-')}`"
               + (f"(代表 `{c['_cluster_rep']}` と同方向)" if c.get("_cluster_rep") else "(代表)" if c.get("_cluster_id") else ""),
               *( [f"- 過去 run と類似: {c['_near_dup']['run']}/{c['_near_dup']['id']}(bigram {c['_near_dup']['score']})"]
                  if c.get("_near_dup") else [] ),
               *( ["- 改訂での主な変更(LLM 自己申告): " + "; ".join(_md_cell(x, 100) for x in lin.get("changes", [])[:5])]
                  if lin.get("changes") else [] ),
               *( ["- 対応した指摘(LLM 自己申告): " + "; ".join(_md_cell(x, 100) for x in lin.get("resolved_red_team_issues", [])[:5])]
                  if lin.get("resolved_red_team_issues") else [] ),
               "",
               "### 3. 評価まとめ(verifier / 各観点)",
               "| 観点 | 評価 | 根拠の強さ |", "|---|---|---|"]
        for ax in axes:
            a = v.get(ax) or {}
            md.append(f"| {lab[ax]} | {_md_cell(a.get('assessment','-'))} | "
                      f"{CONF_LABEL.get(a.get('confidence',''), '-')} |")
        if v.get("notes"):
            md.append(f"\n- verifier メモ: {_md_cell(v['notes'])}")
        if c.get("_llm_kill"):
            md.append(f"- **LLM の棄却推奨理由(要人間確認)**: {_md_cell(c.get('_llm_kill_reason',''))}")

        md += ["", "### 4. AI の指摘(red-team)— 根拠ではなく確認すべき論点",
               "| 種類 | 指摘 | 確認方法 | 現状 |", "|---|---|---|---|"]
        attacks = (c.get("_review") or {}).get("attacks", [])
        for a in attacks:
            mlabel, mstatus = CONVERT_LABEL.get(a.get("convert_to"), (a.get("convert_to", "-"), "-"))
            md.append(f"| {ATTACK_LABEL.get(a.get('type'), a.get('type','-'))} | "
                      f"{_md_cell(a.get('claim',''), 200)} | {mlabel} | {mstatus} |")
        if not attacks:
            if has_fallback_stage(c, "redteam"):
                md.append("| fallback | red-team失敗のため攻撃なしで継続 | 再red-team推奨 | 未検証(fallback) |")
            else:
                md.append("| - | (攻撃記録なし) | - | - |")

        ev = _read_evidence_files(run, cid)
        md += ["", "### 5. 検証・反証の現状(機械的に確認できた事実のみ)",
               "| 確認 | 状況 |", "|---|---|",
               f"| 形式チェック(必須項目) | {'失敗: ' + _md_cell(v.get('kill_reason','')) if v.get('_form_fail') else '通過'} |"]
        for prov in ("arxiv", "inspire", "ntrs"):
            if prov in ev:
                md.append(f"| 文献検索: {prov_names[prov]} | {_provider_status(prov, ev[prov])} |")
        if has_fallback_stage(c, "verify"):
            md.append("| verifier | **未検証(fallback)**: verifier 失敗。verdict は flag として継続し、全観点 low confidence |")
        md += ["| toy 計算・シミュレーション実行 | 未実施(実行系は未実装 / #17) |",
               "| 負例・対照チェック | 未実施 |"]

        md += ["", "### 6. 出典(verifier が採用した近い先行研究)"]
        pa = v.get("prior_art") or []
        if pa:
            md += ["| 信頼度区分 | 文献 | この案との関係 | URL |", "|---|---|---|---|"]
            for p in pa:
                md.append(f"| {TIER_LABEL.get(p.get('source_tier'), p.get('source_tier','-'))} | "
                          f"{_md_cell(p.get('citation',''), 150)} | {_md_cell(p.get('relation',''), 150)} | "
                          f"{_md_cell(p.get('url',''), 80)} |")
        else:
            md.append("(なし)")
        unproven = []
        if not pa:
            # 空の理由は断定できない(ヒット不採用/検索無効/ヒット無し のいずれもありうる)
            unproven.append("新しさの主張(novelty_claim)— verifier が採用した近い先行研究が無く"
                            "**未検証(要確認)**(検索の実施状況は上表、生データは evidence/*.json)")
        unproven.append("実行しやすさ(feasibility)— 机上評価のみ(計算未実施)")
        md += ["", "**根拠がまだ無い主張**: " + " / ".join(unproven),
               "", "### 7. 次の一手",
               f"- 次に深掘りする優先度(配分・採用判定ではない): "
               f"{c.get('_priority','-')} → `{c.get('_next_action','-')}`(内訳は `priority.json`)",
               f"- 最短でダメと分かる確認: {_md_cell(c.get('cheapest_kill'))}",
               "",
               "### Traceability",
               f"`candidates/{cid}.json` → `reviews/{cid}.json` → `revised/{cid}.json` → "
               f"`verdicts/{cid}.json` / `evidence/{cid}.*.json` / `hypothesis_graph.json` / `priority.json`",
               ""]

    md += ["---\n", "## 確認方法の説明(全候補共通)",
           "| 方法 | 何を潰すための確認か | 現 pipeline での扱い |", "|---|---|---|",
           "| 形式チェック | 反証可能な形になっているか(必須項目) | 自動(客観・これだけが自動棄却) |",
           "| 文献検索 | 既に知られている話ではないか | arXiv/INSPIRE/NTRS を自動検索 → verifier が関係を評価 |",
           "| 計算で確認 | 桁・実現性が合うか | **提案のみ**(toy 実行は未実装 / #17) |",
           "| 負例・対照チェック | 偶然や別要因ではないか | **未実施**(次ラウンド以降の人間/将来実装) |",
           "",
           "## 用語対応表(内部キー → このレポートの表現)",
           "| 内部キー | 表現 |", "|---|---|",
           *[f"| `{ax}` | {lab[ax]} |" for ax in axes],
           "| `cheapest_kill` | 最短でダメと分かる確認 |",
           "| `confidence` | 根拠の強さ(低/中/高) |",
           "| `priority_for_next_round` | 次に深掘りする優先度(追加検証の配分。採用判定ではない) |",
           "| `prior_art` | 近い先行研究 |",
           "| `source_tier` | 出典の信頼度区分(権威DB > 査読済み > preprint > 一般web) |",
           ""]
    write_text(run, "candidate_reports.md", "\n".join(md))
    log(run, f"  candidate_reports.md: {len(cands)} 候補の詳細・評価・出典を出力(#47)")


# secrets 様文字列の除外(Issue #48)。提案テキストに混入したら塗りつぶす。
# bearer/authorization は token 本体まで含めて消す(「Bearer xxx」の xxx 残りを防ぐ)
_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer\s+\S+"
    r"|\bbearer\s+\S+"
    r"|\b(?:api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*\S+"
    r"|\b(?:sk-|ghp_|gho_|github_pat_|xoxb-|xoxp-|AKIA)[A-Za-z0-9_\-]{8,})")


def _ps_quote(s, max_len=120):
    """LLM 由来テキストをコピペ用 PowerShell コマンドへ安全に埋め込む:
    空白を正規化 → 単一引用符で囲み、内部の ' は '' にエスケープ(変数展開・$()・` を無効化)。"""
    t = re.sub(r"\s+", " ", str(s)).strip()[:max_len]
    return "'" + t.replace("'", "''") + "'"


def write_memory_suggestions(charter, cands, survivors, run, cfg):
    """memory へ記録すべき候補を run artifact から**決定的に**検知して提案する(Issue #48)。
    自動では保存しない: 提案は『コピペ可能な既存コマンド(promote/reject/prefer)+ evidence』で、
    **実行=承認**(新しい承認 CLI / pending 状態管理は作らない — v1 のレビュー方針)。LLM 不使用。
    会話・GitHub 由来の好み検出は orchestrate からは観測不能(agent 側 / #39 の責務)。"""
    sugs = []

    def add(kind, target, summary, why, evidence, command=""):
        sugs.append({"suggestion_id": f"mem-sug-{len(sugs) + 1:02d}", "kind": kind,
                     "target": target, "summary": _SECRET_RE.sub("[REDACTED]", summary),
                     "why_remember": why, "evidence": evidence,
                     "command": _SECRET_RE.sub("[REDACTED]", command), "status": "pending"})

    rid = run["id"]
    # 1) kill?(LLM) → 理由を人間確認の上 reject 候補(decisions.jsonl へ)
    for c in survivors:
        if c.get("_llm_kill"):
            reason = re.sub(r"\s+", " ", c.get("_llm_kill_reason") or "").strip()[:120]
            add("decision", "memory/decisions.jsonl",
                f"{c['id']} は LLM が棄却を推奨(要確認): {reason}",
                "棄却理由が客観的に正しいか確認し、正しければ reject として記録すると次回から再提案されない。",
                [f"verdicts/{c['id']}.json"],
                f'python orchestrate.py reject {rid} {c["id"]} --note {_ps_quote(reason)}')
    # 2) 生存案の採否の記録(1件にまとめる — 候補ごとに出すとノイズ)
    alive = [c for c in survivors if not c.get("_llm_kill")]
    if alive:
        cmds = "\n".join(
            f"python orchestrate.py promote {rid} {c['id']} --note '<追求する理由>'   # 却下なら reject"
            for c in alive)
        add("decision", "memory/decisions.jsonl",
            f"生存 {len(alive)} 候補({', '.join(c['id'] for c in alive)})の採否を記録する",
            "promote/reject を記録して初めて重複回避・次回生成に効く(記録しないと memory は育たない)。",
            ["decision_matrix.md", "candidate_reports.md"], cmds)
    # 3) 過去 run との重複が反復(≥2件)→ 方針(preference)の記録候補
    dups = [c for c in cands if c.get("_near_dup")]
    if len(dups) >= 2:
        runs_hit = sorted({c["_near_dup"]["run"] for c in dups})
        add("preference", "memory/preferences.md",
            f"過去 run と類似の候補が {len(dups)} 件({', '.join(c['id'] for c in dups)})",
            "同じ方向が繰り返し出ている。深めるなら好みとして、避けるなら回避方針として記録すると生成が変わる。",
            [f"類似元 run: {', '.join(runs_hit[:3])}"],
            "python orchestrate.py prefer '<この方向を深める / 避ける 等の方針>'")
    # 4) engine 失敗の反復(≥2回)→ failure_pattern(issue 化候補)。ログ文字列でなく明示カウンタ(#55)を見る。
    fails = fallback_records(run)
    if len(fails) >= 2:
        add("failure_pattern", "issue",
            f"engine 失敗/フォールバックが {len(fails)} 回発生",
            "反復する失敗は run 固有でなく構造的な可能性。issue 化して原因(timeout/セッション/プロンプト)を追う。",
            ["fallbacks.json", "run.log"],
            f"gh issue create --label bug --title {_ps_quote(f'engine 失敗の反復(run {rid})')} "
            f"--body {_ps_quote('runs/' + rid + '/fallbacks.json と run.log 参照')}")
    # 5) proximity の未探索軸 → 次 run の種 / 好みの候補
    axes_u = (run.get("proximity") or {}).get("underexplored_axes") or []
    if axes_u:
        add("domain_knowledge", "memory/preferences.md",
            "未探索の発想軸: " + "; ".join(str(x) for x in axes_u[:5]),
            "候補集合がまだ触れていない角度。次 run の seed にするか、恒久的な好みにするかは人間が判断。",
            ["proximity.json"],
            "python orchestrate.py prefer '<採用する軸>'  # または次 run の --seed に使う")
    # 6) operator steering の next_round note → 次 run へ持ち越す preference 候補(#53)
    notes = _steering_notes(run)
    revoked = {n.get("revokes") for n in notes if n.get("type") == "revoke" and n.get("status") != "conflicted"}
    next_notes = [n for n in notes
                  if n.get("type", "note") == "note" and n.get("status") != "conflicted"
                  and n.get("id") not in revoked and n.get("scope") == "next_round"]
    for n in next_notes:
        note_text = n.get("note", "")
        add("operator_steering", "memory/preferences.md",
            f"次 run に持ち越す operator steering: {note_text[:120]}",
            "next_round scope の note は research_priority では参考入力になるが evidence ではない。次 run の seed/preference 候補としても人間確認に回す。",
            ["control/operator_notes.jsonl"],
            f"python orchestrate.py prefer {_ps_quote(note_text, 240)}")

    write_json(run, "memory_suggestions.json",
               {"_note": "提案のみ(全件 pending)。自動では memory に保存されない。コマンド実行=承認。",
                "suggestions": sugs})
    md = ["# Memory Suggestions(提案のみ — 自動では保存されない)\n",
          "下の**コマンドを実行すること自体が承認**(既存の promote/reject/prefer が承認メカニズム)。",
          "evidence を確認してから実行する。不要なら何もしなくてよい(放置=却下)。\n"]
    for s in sugs:
        md += [f"## {s['suggestion_id']} [{s['kind']}] → {s['target']}",
               f"- **提案**: {s['summary']}",
               f"- **理由**: {s['why_remember']}",
               f"- **根拠**: {', '.join(s['evidence'])}",
               *(["```powershell", s["command"], "```"] if s["command"] else []),
               ""]
    if not sugs:
        md.append("_(今回の run からの提案は無し)_")
    write_text(run, "memory_suggestions.md", "\n".join(md))
    run["memory_suggestions_n"] = len(sugs)
    log(run, f"  memory suggestions: {len(sugs)} 件を提案(自動保存しない / 実行=承認)")
    return sugs


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


def report(charter, survivors, all_cands, run, cfg):
    _eng = {}
    for c in all_cands:
        _eng[c.get("_engine", "?")] = _eng.get(c.get("_engine", "?"), 0) + 1
    eng_bd = ", ".join(f"{k}:{v}" for k, v in sorted(_eng.items())) or "-"
    write_fallbacks(run)
    fbs = fallback_records(run)
    first_row = next(iter(_priority_rows(run)), {})
    first_id = first_row.get("id")
    grow_recs = (run.get("research_priority") or {}).get("recommendations") or []
    grow_first = sorted(grow_recs, key=lambda r: (r.get("order", 999), r.get("id", "")))[0] if grow_recs else None
    conclusion = [
        f"- seed: **{charter['seed']}**",
        f"- constraints: {charter['constraints'] or '(なし)'}",
        *([f"- domain: {charter['domain']}"] if charter.get("domain") else []),
        f"- engine: {run['engine']} / model: {run['model']}（生成 engine 内訳: {eng_bd}）",
        f"- 生成 {len(all_cands)} / 生存 {len(survivors)} / 客観棄却 {len(all_cands)-len(survivors)}"
        + (f" / LLM 棄却推奨 {sum(1 for c in survivors if c.get('_llm_kill'))} 件(要人間確認)"
           if any(c.get("_llm_kill") for c in survivors) else ""),
        f"- created: {run['created']}",
    ]
    if fbs:
        conclusion.insert(0, f"- ⚠ **この run は {len(fbs)} job が失敗し fallback で継続** — "
                             "red-team/verify が欠けた候補があります(詳細: ## 注意 / `fallbacks.json`)")
    if grow_first:
        conclusion.append(f"- 最初に読む候補(LLM 推奨・要確認): **{grow_first.get('id')}** — "
                          f"{_md_cell(grow_first.get('role'), 120)}。採用判定ではありません。")
    elif first_id:
        conclusion.append(f"- 次に検証する候補(決定的配分): **{first_id}** — "
                          f"`{first_row.get('recommended_next_action', '-')}`。採用判定ではありません。")
    else:
        conclusion.append("- 生存候補が無いため、人間は `discarded.md` と `run.log` を確認してください。")

    text = render(
        load_template("report_summary.md"),
        run_id=run["id"],
        conclusion="\n".join(conclusion),
        verification_order_table=_verification_order_table(survivors, run),
        research_priority_section=_research_priority_section(run),
        first_actions=_first_actions(survivors, run),
        search_quality=_search_quality_section(run, all_cands, cfg, charter),
        warnings=_warnings_section(run, all_cands),
        links=_links_section(),
        memory_suggestions=_memory_suggestions_section(run),
    )
    write_text(run, "REPORT.md", text)


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
    (base / "control").mkdir(exist_ok=True)
    for d in ("candidates", "reviews", "revised", "verdicts", "evidence"):
        (base / d).mkdir(exist_ok=True)
    return {"id": rid, "dir": base, "log": base / "log", "created": _now(),
            "engine": cfg["engine"], "model": cfg["model"], "commit": _git_commit(),
            "_logbuf": [], "_fallbacks": []}


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
    "proximity_enabled": True,        # within-run 重複検知・多様性(#34。注釈のみ)
    "proximity_llm_enabled": True,    # クラスタの theme/警告/未探索軸ラベル(失敗しても決定的注釈は残る)
    "proximity_sim_threshold": 0.45,  # char-bigram Jaccard のクラスタ閾値(cross-run 検知は 0.5)
    "grow_priority_enabled": True,    # Issue #61: 育てる順(LLM 推奨・要確認)。採用判定には使わない
    "lit_search_enabled": True, "lit_search_max_terms": 6,
    "lit_search_max_results": 5, "lit_search_timeout_sec": 15,
    "inspire_enabled": True,   # inspire_mode(always|trigger|off)未指定時はここから導出
    "ntrs_enabled": False,     # NASA NTRS(spacecraft domain で有効化)
    "queue_poll_sec": 3, "queue_timeout_sec": 600,
    "session_warmup_sec": 8, "inject_enter_delay_sec": 1.5,
    "session_buffer_max_chunks": 256,   # TUI 出力バッファの保持上限(チャンク数。診断 tail 用)
    "watchdog_tmp_stale_sec": 60,       # .json.tmp がこの秒数 rename されなければ stale 記録(#46)
    "mock_delay_sec": 0,                # mock steering/watchdog smoke 用。通常は 0
    "mock_fail_kinds": [],              # Issue #55 smoke 用: ["review", "verdict"] など
    "mock_fail_label_contains": [],     # Issue #55 smoke 用: ["__revise_"] など
    # --- 予算プロファイル(Issue #37)。pairwise は全プロファイル 0 既定(opt-in v2 / #36) ---
    # rounds / redteam_per_candidate は charter に記録のみ(rounds>1 の実行は #38)。
    "budget_profile": "",               # "" = 未使用(従来どおり)。quick|normal|deep(--budget で指定)
    "budget_profiles": {
        "quick":  {"n_lenses": 3, "rounds": 1, "redteam_per_candidate": 1, "pairwise_matches": 0},
        "normal": {"n_lenses": 6, "rounds": 1, "redteam_per_candidate": 1, "pairwise_matches": 0},
        # deep の n_lenses はレンズプール(現状6)に cap される。プールを増やせば自動で広がる
        "deep":   {"n_lenses": 12, "rounds": 2, "redteam_per_candidate": 2, "pairwise_matches": 0},
    },
}


def apply_budget_profile(cfg, name):
    """予算プロファイルを cfg に適用(Issue #37)。v1 で実際に効くのは n_lenses のみ
    (レンズプール数で cap)。rounds/redteam_per_candidate/pairwise は charter 記録用
    (multi-round 実行は #38、pairwise は opt-in v2)。明示 CLI(--n-lenses)はこの後で上書きされる。"""
    profiles = cfg.get("budget_profiles") or {}
    p = profiles.get(name)
    if not isinstance(p, dict):
        raise SystemExit(f"budget profile '{name}' が config に無い(候補: {', '.join(profiles) or '(なし)'})")
    pool = len(cfg.get("lenses") or [])
    cfg["n_lenses"] = max(1, min(int(p.get("n_lenses", cfg["n_lenses"])), pool))
    cfg["_budget"] = {"profile": name, **p,
                      "_note": "rounds>1 / redteam_per_candidate>1 の実行は未実装(#38)。pairwise は opt-in v2(#36)。"}
    return cfg


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
    # operator steering channel(Issue #53): 実行中 run への human note を artifact に残す
    if len(sys.argv) > 1 and sys.argv[1] == "steer":
        return cmd_steer(sys.argv[2:])

    ap = argparse.ArgumentParser(description="IDEA-stage funnel MVP (ARCHITECTURE §11)")
    ap.add_argument("--seed", help="研究の種(問い or hunch)")
    ap.add_argument("--seed-file", help="種をファイルから読む")
    ap.add_argument("--constraints", default="", help="制約(使える装置/データ/計算資源 等)")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--engine", choices=["dual", "codex", "claude", "mock"], help="config を上書き")
    ap.add_argument("--budget", choices=["quick", "normal", "deep"],
                    help="予算プロファイル(#37)。n_lenses 等を一括設定(明示 --n-lenses が優先)")
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
    validate_report_templates()

    cfg = load_cfg(args.config)
    cfg["_config_path"] = str(Path(args.config).resolve()) if args.config else "(default)"
    if args.budget or cfg.get("budget_profile"):
        apply_budget_profile(cfg, args.budget or cfg["budget_profile"])
    if args.engine:
        cfg["engine"] = args.engine
    if args.n_lenses:
        cfg["n_lenses"] = args.n_lenses          # 明示 CLI はプロファイルより優先
    if args.no_lit_search:
        cfg["lit_search_enabled"] = False
    if args.engines:
        es = [e.strip() for e in args.engines.split(",") if e.strip()]
        cfg["engines"] = es
        cfg["engine"] = "dual" if len(es) > 1 else (es[0] if es else cfg["engine"])
    if args.timeout:
        cfg["queue_timeout_sec"] = args.timeout

    run = new_run(seed, cfg)
    global EVENTS
    EVENTS = EventLog(run["dir"])   # watchdog(#46): runs/<id>/{events.jsonl,status.json}
    init_operator_control(run)       # operator steering(#53): control/ と ACTIVE.json を初期化
    mem = load_memory()
    print(f"\n=== RUN {run['id']}  (engine={cfg['engine']}) ===")
    print(f"seed: {seed}")
    print(f"memory: 既出候補 {len(mem['seen'])} / 決定 {len(mem['decisions'])} / "
          f"好み {'有' if mem['preferences'] else '無'}\n")

    log(run, "[1/8] PLANNER  — charter 固定")
    charter = planner(seed, args.constraints, cfg, run)
    # 使える engine(実行ファイルが解決できるもの。mock は常に可)を確定。
    # セッションは orchestrator が pywinpty で spawn・駆動する(手動 worker 不要)。
    live = usable_engines(charter["engines"], cfg)
    if not live:
        print(f"使える engine がありません(要求: {charter['engines']})。")
        if not _winpty_available():
            print("  対話セッション駆動には Windows + pywinpty が必要です:"
                  " `pip install -r requirements.txt`(Windows 以外は未対応 / ADR-001)。")
        print("  codex / claude が PATH か既定の場所にあるか確認(新しいターミナルの PATH に入っているか)。")
        print("  配管だけ確認するなら: --engine mock")
        update_operator_state(run, "failed", reason="no usable engine")
        sys.exit(1)
    if live != list(charter["engines"]):
        log(run, f"  注意: 実行ファイル未解決の engine を除外 → 使用 engine {live}(要求 {charter['engines']})")
    charter["engines"] = live
    runner = make_runner_for(live[0], cfg)   # primary。セッションは初回 job で spawn される

    success = False
    try:
        log(run, "[2/8] GENERATE — 発散(独立・並列・memory反映)")
        cands = generate(runner, charter, cfg, run, mem)
        if not cands:
            print("候補が0件。engine/認証/timeout を確認(--engine mock で配管だけ検証可)。")
            sys.exit(1)
        log(run, "[3/8] PROXIMITY— within-run 重複検知・多様性(注釈のみ・棄却しない)")
        cands = proximity(runner, cands, charter, cfg, run)
        log(run, "[4/8] RED-TEAM — 攻撃 -> 検証項目へ変換")
        cands = redteam(runner, cands, cfg, run)
        log(run, "[5/8] REVISE   — 攻撃を受けて仮説を1回だけ改訂")
        cands = revise(runner, cands, cfg, run)
        log(run, "[6/8] VERIFY   — Tier0(形/文献/soundness/feasibility)")
        cands = verify(runner, cands, cfg, run, mem)
        log(run, "[7/8] HARD GATE— kill を捨て案台帳へ")
        survivors = hard_gate(cands, run)
        priority_for_next_round(survivors, charter["eval_axes"], run)   # 配分指針(#37)。採用判定ではない
        log(run, "[8/8] ARBITER  — 整理(勝者は選ばない)")
        arbiter(survivors, run, charter["eval_axes"], cfg)
        research_priority(runner, charter, survivors, cfg, run)          # LLM 推奨・要確認(#61)。matrix には混ぜない
        build_hypothesis_graph(cands, run)          # lineage を graph artifact に集約(#35)
        write_candidate_reports(charter, cands, run, cfg)   # 候補別詳細レポート(#47)
        write_memory_suggestions(charter, cands, survivors, run, cfg)   # 記録候補の提案(#48・保存しない)
        write_unresolved(cands, run)
        write_operator_control(run)                         # operator steering trace(#53)
        report(charter, survivors, cands, run, cfg)
        append_seen(run, cands)                     # 自動メモリ(重複検知用)に追記
        write_text(run, "run.log", "\n".join(run["_logbuf"]))
        success = True
        print(f"\n✅ 完了 → {run['dir']}")
        print(f"   まず読む: {run['dir'] / 'REPORT.md'}  /  {run['dir'] / 'decision_matrix.md'}")
    finally:
        try:
            write_operator_control(run)
            update_operator_state(run, "completed" if success else "failed")
        except Exception:
            pass
        shutdown_runners()                          # spawn した対話セッションを終了


if __name__ == "__main__":
    main()
