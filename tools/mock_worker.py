#!/usr/bin/env python3
"""tools/mock_worker.py — テスト用の偽 queue worker(engine 共通)。

queue/<engine>/inbox のタスクを mock JSON で消化し queue/<engine>/reports に書く。heartbeat も更新。
トークン/常駐セッション不要で queue 配管を検証するためのもの(本番では使わない)。

使い方:
    python tools/mock_worker.py <engine> [run_seconds]      # 例: python tools/mock_worker.py claude 120
別ターミナルで:
    python orchestrate.py --engines mock,claude --no-lit-search --seed "queue test"
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def mock(engine, kind, label):
    if kind == "hypothesis":
        return {"id": label, "question": f"（{engine}-mock の問い）",
                "hypothesis": f"[{engine}-mock] 反証可能な定量的仮説(別エンジン由来)。",
                "novelty_claim": f"最近接は {engine}-mock et al.（要実検索）。",
                "soundness": "保存則に反しない（mock）。",
                "falsification": "測定 X が閾値 Y を超えれば棄却。",
                "test_method": "toy MC / 公開データ再解析。",
                "feasibility": "実現範囲（mock）。",
                "significance_if_true": "Z が更新される。",
                "risk_type": "novelty", "cheapest_kill": "既存データ1点で反証可能。",
                "assumptions": [f"前提（{engine}-mock）"], "unknowns": [f"未知（{engine}-mock）"]}
    if kind == "review":
        return {"attacks": [{"type": "hidden_assumption", "claim": f"前提未明示（{engine}-mock）",
                             "convert_to": "assumption", "pointer": ""}]}
    return {"novelty": {"assessment": f"中（{engine}-mock）", "confidence": "low"},
            "soundness": {"assessment": "可（mock）", "confidence": "low"},
            "feasibility": {"assessment": "可（mock）", "confidence": "low"},
            "significance": {"assessment": "中（mock）", "confidence": "low"},
            "prior_art": [], "verdict": "keep", "kill_reason": "", "notes": f"{engine}-mock"}


def main():
    if len(sys.argv) < 2:
        print("usage: python tools/mock_worker.py <engine> [run_seconds]")
        sys.exit(1)
    engine = sys.argv[1]
    run_sec = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    q = ROOT / "queue"
    (q / engine / "inbox").mkdir(parents=True, exist_ok=True)
    (q / engine / "reports").mkdir(parents=True, exist_ok=True)
    alive = q / f"{engine}.alive"
    print(f"[mock_worker:{engine}] servicing {q / engine} for {run_sec}s …", flush=True)
    t0, n = time.time(), 0
    while time.time() - t0 < run_sec:
        alive.write_text(str(time.time()), encoding="utf-8")   # heartbeat
        for f in sorted((q / engine / "inbox").glob("*.json")):
            rep = q / engine / "reports" / f.name
            if rep.exists():
                continue
            try:
                task = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            rep.write_text(json.dumps(mock(engine, task.get("kind"), task.get("label", "")),
                                      ensure_ascii=False), encoding="utf-8")
            n += 1
            print(f"  serviced {f.name} (kind={task.get('kind')})", flush=True)
        time.sleep(1)
    print(f"[mock_worker:{engine}] done. serviced {n} tasks.", flush=True)


if __name__ == "__main__":
    main()
