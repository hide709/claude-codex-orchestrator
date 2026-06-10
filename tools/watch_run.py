#!/usr/bin/env python3
"""watch_run.py — 実行中 run の engine 状態を表示する watcher(Issue #46)。

orchestrator が書く runs/<id>/status.json(現在状態)と events.jsonl(遷移履歴)を
**読むだけ**(run には一切影響しない)。LLM がこけた/承認待ち/脱線 を別ターミナルから把握する用。

使い方:
  python tools/watch_run.py             # 最新 run を 3 秒間隔で表示
  python tools/watch_run.py --once      # 1回だけ表示
  python tools/watch_run.py runs/<id>   # run を指定
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def latest_run():
    runs = sorted((ROOT / "runs").glob("*"), key=lambda p: p.name, reverse=True)
    return runs[0] if runs else None


def _tail_lines(p, n=8, chunk=16384):
    """末尾 chunk バイトだけ読んで最後の n 行を返す(長い run でも全文読みしない)。"""
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > chunk and lines:
        lines = lines[1:]      # 途中から読んだ欠け行を捨てる
    return lines[-n:]


def show(rd):
    print(f"\n=== {rd.name} @ {time.strftime('%H:%M:%S')} ===")
    st_p = rd / "status.json"
    if st_p.exists():
        try:
            st = json.loads(st_p.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        print(f"{'ENGINE':8} {'STATE':20} {'JOB':44} {'BYTES':>9} {'OUT_AGE':>7}  HINT")
        for eng, s in st.items():
            print(f"{eng:8} {str(s.get('state','-')):20} {str(s.get('label','-'))[:44]:44} "
                  f"{str(s.get('bytes_total','-')):>9} {str(s.get('last_output_age_sec','-')):>7}  "
                  f"{s.get('hint','')}")
    else:
        print("(status.json なし — run 直後か、watchdog 以前の run)")
    ev_p = rd / "events.jsonl"
    if ev_p.exists():
        print("--- 直近イベント ---")
        for line in _tail_lines(ev_p, 8):
            try:
                e = json.loads(line)
                extra = {k: v for k, v in e.items() if k not in ("ts", "engine", "label", "event")}
                print(f"{e.get('ts','')}  {e.get('engine',''):7} {e.get('event',''):22} "
                      f"{e.get('label','')}" + (f"  {extra}" if extra else ""))
            except Exception:
                pass


def main():
    args = sys.argv[1:]
    once = "--once" in args
    args = [a for a in args if a != "--once"]
    rd = Path(args[0]) if args else latest_run()
    if not rd or not Path(rd).exists():
        print("runs/ に run がありません")
        return
    rd = Path(rd)
    while True:
        show(rd)
        if once:
            break
        time.sleep(3)


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        main()
    except KeyboardInterrupt:
        pass
