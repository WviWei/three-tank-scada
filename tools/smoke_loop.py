"""闭环冒烟测试: plant + PLC 联调, 验证双回路能否稳定到设定值。

判定标准:
  * T1、T2 最终值进入设定值 +/-12 mm 死区;
  * 全程不发生溢流;
  * PLC 心跳持续推进(证明扫描周期正常)。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import AsyncModbusTcpClient  # noqa: E402

from common import registers as R  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 工作点选在泵 77% / 51% 附近, 两路均留出调节余量。
# 若 SP1 取 200 mm, 稳态需要泵1 约 91%, 逼近饱和, 控制品质必然下降。
SP1, SP2 = int(os.environ.get("SMOKE_SP1", "150")), int(os.environ.get("SMOKE_SP2", "130"))
DURATION = int(os.environ.get("SMOKE_SECONDS", "95"))


async def wait_port(host: str, port: int, timeout: float = 15.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, w = await asyncio.open_connection(host, port)
            w.close()
            await w.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.2)
    raise TimeoutError(f"端口 {port} 未就绪")


def spawn(module: str, env) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


async def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    plant = spawn("plant.plant", env)
    ok = True
    plc = None
    try:
        await wait_port(R.PLANT_HOST, R.PLANT_PORT)
        plc = spawn("controller.plc", env)
        await asyncio.sleep(1.5)

        client = AsyncModbusTcpClient(R.PLANT_HOST, port=R.PLANT_PORT)
        await client.connect()
        # 先让装置在手动工况下稳定, 再切自动 —— 真实的开车顺序
        await asyncio.sleep(10)
        await client.write_register(R.HR["sp1_mm"], SP1, slave=0)
        await client.write_register(R.HR["sp2_mm"], SP2, slave=0)
        await client.write_register(R.HR["plc_mode"], 1, slave=0)
        print(f"[1] 已切 AUTO, SP1={SP1} mm, SP2={SP2} mm")

        hb_prev = None
        hb_ok = True
        samples = []
        step = 5
        for t in range(0, DURATION + 1, step):
            rr = await client.read_input_registers(0, count=R.IR_SIZE, slave=0)
            hr = await client.read_holding_registers(0, count=R.HR_SIZE, slave=0)
            if rr.isError() or hr.isError():
                print("读失败")
                ok = False
                break
            v, h = rr.registers, hr.registers
            hb = h[R.HR["plc_heartbeat"]]
            if hb_prev is not None and hb == hb_prev:
                hb_ok = False
            hb_prev = hb
            row = (
                t,
                v[R.IR["h1_mm"]],
                v[R.IR["h2_mm"]],
                v[R.IR["h3_mm"]],
                h[R.HR["pid1_out"]],
                h[R.HR["pid2_out"]],
                h[R.HR["plc_alarm_word"]],
            )
            samples.append(row)
            print(
                f"    t={t:3d}s h1={row[1]:4d} h2={row[2]:4d} h3={row[3]:4d} "
                f"| u1={row[4]:4d} u2={row[5]:4d} alarm=0x{row[6]:02X}"
            )
            if t < DURATION:
                await asyncio.sleep(step)

        print(f"[2] PLC 心跳推进: {'OK' if hb_ok else 'FAIL'}")
        ok &= hb_ok

        if samples:
            h1, h2 = samples[-1][1], samples[-1][2]
            e1, e2 = abs(h1 - SP1), abs(h2 - SP2)
            print(f"[3] 稳态误差: T1={e1} mm, T2={e2} mm (死区 12 mm)")
            ok &= e1 <= 12 and e2 <= 12

            max_h = max(max(s[1], s[2], s[3]) for s in samples)
            print(f"[4] 全程最高液位 {max_h} mm, 溢流={'否' if max_h < R.H_FULL_MM else '是'}")
            ok &= max_h < R.H_FULL_MM

            peak1 = max(s[1] for s in samples)
            print(f"[5] T1 超调: 峰值 {peak1} mm, 超调量 {max(0, peak1 - SP1) / SP1 * 100:.1f}%")

        client.close()
    finally:
        for proc in (plc, plant):
            if proc:
                proc.terminate()
        for proc in (plc, plant):
            if proc:
                try:
                    out, _ = proc.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
                name = "PLC" if proc is plc else "PLANT"
                for line in out.decode("utf-8", "replace").strip().splitlines()[-3:]:
                    print(f"   [{name}] {line}")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
