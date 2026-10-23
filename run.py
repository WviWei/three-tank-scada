"""一键启动: 现场对象 -> 软 PLC -> SCADA 上位机。

三个进程按工业现场的顺序拉起, 任何一个退出则全部停止。
    python run.py            正常启动
    python run.py --no-plc   只起对象和上位机(手动模式调试用)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def spawn(module: str, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=ROOT,
        env=env,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="启动三容水箱 SCADA 演示系统")
    ap.add_argument("--no-plc", action="store_true", help="不启动软 PLC")
    args = ap.parse_args()

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    procs: list[tuple[str, subprocess.Popen]] = []

    def start(name: str, module: str, wait: float = 0.0) -> None:
        proc = spawn(module, env)
        procs.append((name, proc))
        print(f"[start] {name:<8} pid={proc.pid}  python -m {module}", flush=True)
        if wait:
            time.sleep(wait)

    try:
        start("plant", "plant.plant", wait=1.5)
        if not args.no_plc:
            start("plc", "controller.plc", wait=0.8)
        start("scada", "scada.main", wait=0.5)

        print("\n  SCADA 已启动: http://127.0.0.1:8000\n  Ctrl+C 停止全部进程\n", flush=True)

        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    print(f"[exit] {name} 意外退出, 返回码 {code}", flush=True)
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[stop] 正在停止...", flush=True)
        return 0
    finally:
        for name, proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 8
        for name, proc in reversed(procs):
            try:
                proc.wait(timeout=max(0.5, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()
        print("[stop] 全部进程已退出", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
