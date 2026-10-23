"""对象仿真的冒烟测试: 拉起 plant 子进程, 通过 Modbus 写入泵指令并读取响应。

验证三件事:
  1. Modbus TCP 从站可连通, 寄存器地址与 common/registers.py 的契约一致;
  2. 泵指令写入后液位按物理规律上升(方向正确);
  3. 50 ms 仿真周期下数值稳定, 不出现 NaN / 溢出。
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


async def wait_port(host: str, port: int, timeout: float = 15.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            _, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.2)
    raise TimeoutError(f"plant 端口 {host}:{port} 在 {timeout}s 内未就绪")


async def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "plant.plant"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    ok = True
    try:
        await wait_port(R.PLANT_HOST, R.PLANT_PORT)

        client = AsyncModbusTcpClient(R.PLANT_HOST, port=R.PLANT_PORT)
        connected = await client.connect()
        print(f"[1] Modbus 连接: {'OK' if connected else 'FAIL'}")
        ok &= connected

        await client.write_register(R.HR["pump1_permille"], 600, slave=0)
        await client.write_register(R.HR["pump2_permille"], 400, slave=0)
        print("[2] 已下发 泵1=60% 泵2=40%")

        samples = []
        for _ in range(12):
            await asyncio.sleep(1.0)
            rr = await client.read_input_registers(0, count=R.IR_SIZE, slave=0)
            if rr.isError():
                print(f"读失败: {rr}")
                ok = False
                break
            v = rr.registers
            samples.append(
                (v[R.IR["h1_mm"]], v[R.IR["h2_mm"]], v[R.IR["h3_mm"]], v[R.IR["q1_lpm100"]])
            )

        for i, (h1, h2, h3, q1) in enumerate(samples):
            print(f"    t+{i + 1:2d}s  h1={h1:4d}mm h2={h2:4d}mm h3={h3:4d}mm q1={q1 / 100:.2f}L/min")

        if samples:
            rising = samples[-1][0] > samples[0][0]
            print(f"[3] 液位响应方向: {'OK (上升)' if rising else 'FAIL (未上升)'}")
            ok &= rising
            q_last = samples[-1][3] / 100.0
            # 泵1 指令 60%, 单泵量程 9 L/min => 稳态应趋近 5.40 L/min
            print(f"[4] 泵1 实际流量 {q_last:.2f} L/min (期望趋近 5.40)")
            ok &= 5.2 < q_last < 5.6

        client.close()
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        tail = out.decode("utf-8", "replace").strip().splitlines()
        print("--- plant 日志尾部 ---")
        for line in tail[-6:]:
            print("   ", line)

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
