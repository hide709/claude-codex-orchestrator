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
        return [exe, "-c", "service_tier=flex", "-c", f"model_reasoning_effort={cfg.get('reasoning_effort', 'low')}",
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
        (logdir / f"{label}.log.txt").write_text(f"[mock] {label}\n{prompt[:400]}", encoding="utf-8")
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
    by_id = {r["id"]: r for r in rows}
    for c in survivors:
        c["_priority"] = by_id[c["id"]]["priority_for_next_round"]
        c["_next_action"] = by_id[c["id"]]["recommended_next_action"]
    log(run, f"  priority: 追加予算の配分指針を {len(rows)} 件に付与(採用判定ではない / floor 保証)")
    return rows


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
            "id": c["id"], "lens": c.get("_lens", "?"), "cluster": c.get("_cluster_id", "-"),
            "engine": c.get("_engine", "?"),
            "verdict": status, "risk_type": c.get("risk_type", ""),
            "hypothesis": c.get("hypothesis", ""),
            "cheapest_kill": c.get("cheapest_kill", ""),
            # 配分指針(#37)。採用判定ではない(凡例参照)
            "next_round": (f'{c["_priority"]} → {c.get("_next_action", "-")}'
                           if "_priority" in c else "-"),
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

    header = ("| id | lens | cluster | engine | verdict | risk | " + " | ".join(axes)
              + " | cheapest_kill | next_round(配分) |")
    sep = "|" + "---|" * (8 + len(axes))
    md = ["# decision_matrix (人間が読む)\n",
          "**勝者は選んでいない。** AIは候補を出し客観検証しただけ。",
          "どの生存案に *実験予算* を割くかは人間が決める(ARCHITECTURE §3.7 / §5)。\n",
          "verdict 凡例: `keep` / `flag`(通説違反など要注目で残す) / "
          "`kill?(LLM/要確認)`=LLM は kill 推奨だが客観未確認 → 人間が棄却の妥当性を判断。\n",
          "next_round(配分)凡例: 次ラウンドで**追加**の検証予算をどこに使うかの決定的な指針"
          "(`priority.json` に内訳)。**採用判定ではない** — 低くても棄却されない(floor / #37)。\n",
          header, sep]
    for r in rows:
        cells = [r["id"], r["lens"], r["cluster"], r["engine"], r["verdict"], r["risk_type"]]
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
          *(["## 多様性(within-run proximity / 注釈のみ・棄却しない)",
             *[f"- {cl['cluster_id']}『{cl.get('theme') or '-'}』: {', '.join(cl['members'])}"
               + (f" / 代表 {cl['representative']}" if len(cl['members']) > 1 else "")
               + (f" / ⚠ {cl['diversity_warning']}" if cl.get('diversity_warning') else "")
               for cl in run.get("proximity", {}).get("clusters", [])],
             *([f"- 未探索軸(次 run の種候補): {', '.join(run['proximity']['underexplored_axes'])}"]
               if run.get("proximity", {}).get("underexplored_axes") else []), ""]
            if run.get("proximity") else []),
          "## 読み方",
          "1. `decision_matrix.md` … 生存候補を評価軸ごとに(単一スコアに潰さず)一覧。",
          "2. `candidates/*.json` … 各 Research Hypothesis Contract。",
          "3. `reviews/*.json` … red-team の攻撃(検証項目へ変換済み)。",
          "4. `revised/*.json` … red-team 後の改訂版。原案は `candidates/` に残る。",
          "5. `evidence/*.json` … arXiv(preprint)/INSPIRE-HEP/NASA NTRS(権威DB)から機械的に収集した検証補助データ。",
          "6. `verdicts/*.json` … Tier0 検証結果(novelty/soundness/feasibility + prior_art)。",
          "7. `proximity.json` … within-run の重複クラスタ・多様性・未探索軸(#34。注釈のみ)。",
          "8. `hypothesis_graph.{json,md}` … 仮説の lineage(seed→生成→攻撃→改訂→検証 / #35)。",
          "9. `priority.json` … 次ラウンドの追加検証予算の配分指針(#37。**採用判定ではない**・内訳付き)。",
          "10. `discarded.md` … hard gate 落ち(理由付き)。 `unresolved.md` … 未解決・未追跡変種。",
          "",
          "## 次の一手(人間)",
          "- 生存案のうち `cheapest_kill` が安いものから Tier1(toy計算/既存データ再解析)に回す。",
          "- matrix の `next_round(配分)` / `priority.json` は追加検証の配分指針(採用判定ではない)。"
          "不確実性の高い軸から潰すのに使う。",
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
    "proximity_enabled": True,        # within-run 重複検知・多様性(#34。注釈のみ)
    "proximity_llm_enabled": True,    # クラスタの theme/警告/未探索軸ラベル(失敗しても決定的注釈は残る)
    "proximity_sim_threshold": 0.45,  # char-bigram Jaccard のクラスタ閾値(cross-run 検知は 0.5)
    "lit_search_enabled": True, "lit_search_max_terms": 6,
    "lit_search_max_results": 5, "lit_search_timeout_sec": 15,
    "inspire_enabled": True,   # inspire_mode(always|trigger|off)未指定時はここから導出
    "ntrs_enabled": False,     # NASA NTRS(spacecraft domain で有効化)
    "queue_poll_sec": 3, "queue_timeout_sec": 600,
    "session_warmup_sec": 8, "inject_enter_delay_sec": 1.5,
    "session_buffer_max_chunks": 256,   # TUI 出力バッファの保持上限(チャンク数。診断 tail 用)
    "watchdog_tmp_stale_sec": 60,       # .json.tmp がこの秒数 rename されなければ stale 記録(#46)
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

    cfg = load_cfg(args.config)
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
        sys.exit(1)
    if live != list(charter["engines"]):
        log(run, f"  注意: 実行ファイル未解決の engine を除外 → 使用 engine {live}(要求 {charter['engines']})")
    charter["engines"] = live
    runner = make_runner_for(live[0], cfg)   # primary。セッションは初回 job で spawn される

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
        arbiter(survivors, run, charter["eval_axes"])
        build_hypothesis_graph(cands, run)          # lineage を graph artifact に集約(#35)
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
