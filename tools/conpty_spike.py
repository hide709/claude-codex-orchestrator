#!/usr/bin/env python3
"""
conpty_spike.py — pywinpty 版。外部駆動 interactive runner が成立するかの最小検証(Windows)。

  echo : 子プロセスの stdout が PTY 経由で読めるか(ctypes 版で失敗した点)
  codex: codex を対話起動 → job1(seed)で report 生成 → job2 を注入して report 生成
         (= supervisor が job 単位で wake/inject/observe できるか)

使い方:
  python tools/conpty_spike.py echo
  python tools/conpty_spike.py codex "<codex.exe のフルパス>"
依存: pywinpty(stdlib 縛りを外して採用)。
"""
import sys
import threading
import time
from pathlib import Path

from winpty import PtyProcess

ROOT = Path(__file__).resolve().parent.parent


class Reader:
    """PtyProcess の出力を別スレッドで読み続けてバッファに溜める。"""
    def __init__(self, proc):
        self.proc = proc
        self._chunks = []
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                data = self.proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if data:
                with self._lock:
                    self._chunks.append(data)
            else:
                time.sleep(0.05)

    def text(self):
        with self._lock:
            return "".join(self._chunks)


def wait_file(p, timeout, poll=2):
    p = Path(p)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if p.exists():
            return True
        time.sleep(poll)
    return False


def mode_echo():
    print("[echo] pywinpty で子プロセスを起動し、stdout を PTY 経由で読む…")
    proc = PtyProcess.spawn(
        ["python", "-c", "import time; print('HELLO_CONPTY', flush=True); time.sleep(8)"],
        cwd=str(ROOT), dimensions=(30, 120))
    r = Reader(proc)
    for _ in range(10):
        time.sleep(1)
        if "HELLO_CONPTY" in r.text():
            break
    out = r.text()
    ok = "HELLO_CONPTY" in out
    print(f"[echo] alive={proc.isalive()} bytes={len(out)} marker={ok}")
    print("---- tail ----\n" + repr(out[-300:]))
    try:
        proc.terminate(force=True)
    except Exception:
        pass
    print("RESULT:", "PASS" if ok else "FAIL")


def _launch_argv(engine, exe, job):
    if engine == "codex":
        return [exe, "-c", "service_tier=flex", "-s", "workspace-write", "-a", "never", job]
    if engine == "claude":
        return [exe, "--permission-mode", "acceptEdits", job]
    raise ValueError(f"unknown engine {engine}")


def mode_engine(engine, exe):
    qrep = ROOT / "queue" / engine / "reports"
    qrep.mkdir(parents=True, exist_ok=True)
    for f in ("spike1.json", "spike2.json"):
        (qrep / f).unlink(missing_ok=True)

    job1 = (f'queue/{engine}/reports/spike1.json に、説明やコードフェンス無しで '
            'JSON {"ok": true, "job": 1} だけを書いてください。書けたら SPIKE_DONE_1 と出力。')
    print(f"[{engine}] 起動: {exe} (interactive, seed=job1)")
    proc = PtyProcess.spawn(_launch_argv(engine, exe, job1), cwd=str(ROOT), dimensions=(40, 140))
    r = Reader(proc)

    print(f"[{engine}] step1(seed): spike1.json を待つ(最大300s)…")
    ok1 = wait_file(qrep / "spike1.json", 300)
    print("  step1:", "PASS" if ok1 else "FAIL")

    ok2 = False
    if ok1:
        job2 = (f'次に queue/{engine}/reports/spike2.json へ、説明無しで JSON {{"ok": true, "job": 2}} '
                'だけを書いて、SPIKE_DONE_2 と出力してください。')
        print(f"[{engine}] step2(inject): job2 をテキスト注入 → Enter を別キーで送る…")
        proc.write(job2)
        time.sleep(1.5)
        proc.write("\r")                      # Enter を paste と分離して送る
        ok2 = wait_file(qrep / "spike2.json", 150)
        if not ok2:
            print("  (retry Enter: \\r\\n)")
            proc.write("\r\n")
            ok2 = wait_file(qrep / "spike2.json", 120)
        print("  step2:", "PASS" if ok2 else "FAIL")

    out = r.text()
    try:
        proc.terminate(force=True)
    except Exception:
        pass
    print(f"---- {engine} TUI tail(800) ----\n" + out[-800:])
    print(f"RESULT[{engine}]: step1={'PASS' if ok1 else 'FAIL'} step2(inject)={'PASS' if ok2 else 'FAIL'}")


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    if mode == "echo":
        mode_echo()
    elif mode in ("codex", "claude"):
        if len(sys.argv) < 3:
            print(f'usage: python tools/conpty_spike.py {mode} "<{mode}.exe path>"')
            sys.exit(1)
        mode_engine(mode, sys.argv[2])
    else:
        print("unknown mode")
