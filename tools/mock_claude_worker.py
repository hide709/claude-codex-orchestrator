#!/usr/bin/env python3
"""tools/mock_claude_worker.py — テスト用の偽 Claude queue worker。

queue/inbox のタスクを mock JSON で消化し queue/reports に書く。heartbeat も更新する。
トークン/常駐 Claude 不要で dual-engine の queue 配管を検証するためのもの(本番では使わない)。

使い方:
    python tools/mock_claude_worker.py [run_seconds]   # 既定 30 秒
別ターミナルで:
    python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
Q = ROOT / "queue"
(Q / "inbox").mkdir(parents=True, exist_ok=True)
(Q / "reports").mkdir(parents=True, exist_ok=True)


def mock(kind, label):
    if kind == "hypothesis":
        return {"id": label, "question": "（claude-mock の問い）",
                "hypothesis": "[claude-mock] 反証可能な定量的仮説(別エンジン由来)。",
                "novelty_claim": "最近接は claude-mock et al.（要実検索）。",
                "soundness": "保存則に反しない（claude-mock）。",
                "falsification": "測定 X が閾値 Y を超えれば棄却。",
                "test_method": "toy MC / 公開データ再解析。",
                "feasibility": "実現範囲（claude-mock）。",
                "significance_if_true": "Z が更新される。",
                "risk_type": "novelty", "cheapest_kill": "既存データ1点で反証可能。",
                "assumptions": ["前提（claude-mock）"], "unknowns": ["未知（claude-mock）"]}
    if kind == "review":
        return {"attacks": [{"type": "hidden_assumption", "claim": "前提未明示（claude-mock）",
                             "convert_to": "assumption", "pointer": ""}]}
    return {"novelty": {"assessment": "中（claude-mock）", "confidence": "low"},
            "soundness": {"assessment": "可（claude-mock）", "confidence": "low"},
            "feasibility": {"assessment": "可（claude-mock）", "confidence": "low"},
            "significance": {"assessment": "中（claude-mock）", "confidence": "low"},
            "prior_art": [], "verdict": "keep", "kill_reason": "", "notes": "claude-mock"}


def main():
    run_sec = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"[mock_claude_worker] servicing {Q} for {run_sec}s …", flush=True)
    t0, n = time.time(), 0
    while time.time() - t0 < run_sec:
        (Q / "claude.alive").write_text(str(time.time()), encoding="utf-8")   # heartbeat
        for f in sorted((Q / "inbox").glob("*.json")):
            rep = Q / "reports" / f.name
            if rep.exists():
                continue
            try:
                task = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rep.write_text(json.dumps(mock(task.get("kind"), task.get("label", "")),
                                      ensure_ascii=False), encoding="utf-8")
            n += 1
            print(f"  serviced {f.name} (kind={task.get('kind')})", flush=True)
        time.sleep(1)
    print(f"[mock_claude_worker] done. serviced {n} tasks.", flush=True)


if __name__ == "__main__":
    main()
