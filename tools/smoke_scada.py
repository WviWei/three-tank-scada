"""端到端集成测试: 拉起完整的三个进程, 通过 SCADA 的 HTTP 接口验证全链路。

验证:
  1. /api/state 能返回结构化实时数据;
  2. 通过 /api/command 切 AUTO 后, 液位收敛到设定值;
  3. /api/history 有历史采样入库;
  4. 静态页面可访问。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "http://127.0.0.1:8000"
SP1, SP2 = 150, 130
WAIT = int(os.environ.get("SCADA_SMOKE_SECONDS", "70"))


def http(path: str, payload: dict | None = None, timeout: float = 5.0):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        return json.loads(body) if "json" in ctype else body.decode("utf-8", "replace")


def wait_http(timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http("/api/state", timeout=2.0)
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(0.5)
    return False


def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    procs = []
    ok = True

    def start(mod: str) -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, "-m", mod],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
        return p

    try:
        start("plant.plant")
        time.sleep(2.0)
        start("controller.plc")
        time.sleep(1.0)
        start("scada.main")

        if not wait_http():
            print("[FAIL] SCADA HTTP 服务未在 40 s 内就绪")
            return 1
        print("[1] SCADA HTTP 就绪")

        page = http("/")
        has_echarts = "echarts" in page
        print(f"[2] 首页可访问, 包含图表引用: {'是' if has_echarts else '否'}")
        ok &= has_echarts

        # 让装置在手动工况稳定后再切自动
        time.sleep(8)
        http("/api/command", {"sp1": SP1, "sp2": SP2, "mode": 1})
        print(f"[3] 已下发 AUTO, SP1={SP1} SP2={SP2}")

        t0 = time.time()
        while time.time() - t0 < WAIT:
            time.sleep(5)
            s = http("/api/state")
            if not s.get("levels"):
                print("    等待首次采样...")
                continue
            lv = s["levels"]
            print(
                f"    t={int(time.time() - t0):3d}s h1={lv['h1']:4d} h2={lv['h2']:4d} "
                f"h3={lv['h3']:4d} u1={s['pumps']['u1']:4d} u2={s['pumps']['u2']:4d} "
                f"alarm=0x{s['alarm_word']:02X}"
            )

        s = http("/api/state")
        lv = s.get("levels", {})
        e1, e2 = abs(lv.get("h1", 0) - SP1), abs(lv.get("h2", 0) - SP2)
        print(f"[4] 稳态误差: T1={e1} mm, T2={e2} mm (死区 12 mm)")
        ok &= e1 <= 12 and e2 <= 12

        hist = http("/api/history?seconds=300")
        print(f"[5] 历史采样 {len(hist)} 条")
        ok &= len(hist) > 10

        plc_online = s.get("plc", {}).get("online", False)
        print(f"[6] PLC 在线标志: {'OK' if plc_online else 'FAIL'}")
        ok &= bool(plc_online)

    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for p in reversed(procs):
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
