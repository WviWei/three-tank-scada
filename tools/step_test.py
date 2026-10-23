"""阶跃响应对比实验: 静态解耦 ON vs OFF。

实验设计
--------
1. 装置以手动工况(泵1=50%, 泵2=45%)开车, 待液位稳定;
2. 切 AUTO, SP1=150 mm, SP2=100 mm, 等待闭环稳定;
3. 保持 SP2 不动, 把 SP1 阶跃到 175 mm;
4. 记录 T1 的跟踪响应与 T2 的受扰响应。

评价指标
--------
  T1 超调量     : (峰值 - 阶跃目标) / 阶跃目标 * 100%
  T1 调节时间   : 进入并保持在 +/-2% 误差带所需时间
  T2 受扰峰值   : 阶跃期间 T2 偏离基线绝对值的最大值  <-- 耦合强弱的核心指标

若解耦有效, T2 受扰峰值应显著下降。
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import AsyncModbusTcpClient  # noqa: E402

from common import registers as R  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

SP1_BASE, SP2_BASE = 150, 130
SP1_STEP = 175
SETTLE_S = 62      # 闭环稳定等待时间
OBSERVE_S = 72     # 阶跃后观测时间
SAMPLE_S = 0.5


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


async def one_run(decouple: bool) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    plant = subprocess.Popen(
        [sys.executable, "-m", "plant.plant"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    plc = None
    tag = "解耦 ON " if decouple else "解耦 OFF"
    try:
        await wait_port(R.PLANT_HOST, R.PLANT_PORT)
        plc_args = [sys.executable, "-m", "controller.plc"]
        if not decouple:
            plc_args.append("--no-decouple")
        plc = subprocess.Popen(
            plc_args, cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await asyncio.sleep(2.5)

        client = AsyncModbusTcpClient(R.PLANT_HOST, port=R.PLANT_PORT, timeout=1.0)
        await client.connect()
        await client.write_register(R.HR["sp1_mm"], SP1_BASE, slave=0)
        await client.write_register(R.HR["sp2_mm"], SP2_BASE, slave=0)
        await client.write_register(R.HR["plc_mode"], 1, slave=0)
        print(f"  [{tag}] AUTO 已投入, 稳定中 {SETTLE_S}s ...", flush=True)

        await asyncio.sleep(SETTLE_S)

        # 基线: 取阶跃前 8 秒平均
        base = []
        for _ in range(16):
            rr = await client.read_input_registers(0, count=R.IR_SIZE, slave=0)
            base.append((rr.registers[R.IR["h1_mm"]], rr.registers[R.IR["h2_mm"]]))
            await asyncio.sleep(SAMPLE_S)
        h1_base = statistics.fmean(x[0] for x in base)
        h2_base = statistics.fmean(x[1] for x in base)
        print(f"  [{tag}] 阶跃前基线 h1={h1_base:.1f} h2={h2_base:.1f}", flush=True)

        # 阶跃
        await client.write_register(R.HR["sp1_mm"], SP1_STEP, slave=0)
        t_step = asyncio.get_event_loop().time()

        samples = []
        while asyncio.get_event_loop().time() - t_step < OBSERVE_S:
            rr = await client.read_input_registers(0, count=R.IR_SIZE, slave=0)
            hr = await client.read_holding_registers(0, count=R.HR_SIZE, slave=0)
            v, h = rr.registers, hr.registers
            samples.append({
                "t": round(asyncio.get_event_loop().time() - t_step, 2),
                "h1": v[R.IR["h1_mm"]],
                "h2": v[R.IR["h2_mm"]],
                "h3": v[R.IR["h3_mm"]],
                "u1": h[R.HR["pid1_out"]],
                "u2": h[R.HR["pid2_out"]],
            })
            await asyncio.sleep(SAMPLE_S)

        client.close()
        return analyze(samples, h1_base, h2_base, decouple)
    finally:
        for p in (plc, plant):
            if p and p.poll() is None:
                p.terminate()
        for p in (plc, plant):
            if p:
                try:
                    p.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    p.kill()


def analyze(samples: list[dict], h1_base: float, h2_base: float, decouple: bool) -> dict:
    step_amp = SP1_STEP - h1_base
    peak = max(s["h1"] for s in samples)
    overshoot = max(0.0, (peak - SP1_STEP) / step_amp * 100.0)

    band = 0.02 * (SP1_STEP - h1_base)
    settle_t = None
    for i in range(len(samples)):
        if all(abs(s["h1"] - SP1_STEP) <= band for s in samples[i:]):
            settle_t = samples[i]["t"]
            break

    h2_dev = max(abs(s["h2"] - h2_base) for s in samples)
    final_h1 = samples[-1]["h1"]
    final_h2 = samples[-1]["h2"]

    return {
        "decouple": decouple,
        "h1_base": round(h1_base, 1),
        "h2_base": round(h2_base, 1),
        "sp1_step": SP1_STEP,
        "sp2": SP2_BASE,
        "peak_h1": peak,
        "overshoot_pct": round(overshoot, 2),
        "settle_s": settle_t,
        "h2_disturbance_mm": h2_dev,
        "final_h1": final_h1,
        "final_h2": final_h2,
        "steady_err_h1": final_h1 - SP1_STEP,
        "steady_err_h2": final_h2 - SP2_BASE,
        "samples": samples,
    }


async def main() -> int:
    os.makedirs(DOCS, exist_ok=True)
    results = []
    for decouple in (True, False):
        print(f"\n===== 实验: {'解耦开启' if decouple else '解耦关闭'} =====", flush=True)
        results.append(await one_run(decouple))

    on, off = results[0], results[1]

    rows = [
        ("T1 超调量 (%)", f"{on['overshoot_pct']}"), 
        ("T1 调节时间 (s)", f"{on['settle_s']}"),
        ("T2 受扰峰值 (mm)", f"{on['h2_disturbance_mm']}"),
        ("T1 稳态误差 (mm)", f"{on['steady_err_h1']}"),
        ("T2 稳态误差 (mm)", f"{on['steady_err_h2']}"),
    ]

    print("\n" + "=" * 62)
    print(f"{'指标':<20}{'解耦 ON':>16}{'解耦 OFF':>16}")
    print("-" * 62)
    for i, (name, _) in enumerate(rows):
        a = [on['overshoot_pct'], on['settle_s'], on['h2_disturbance_mm'],
             on['steady_err_h1'], on['steady_err_h2']][i]
        b = [off['overshoot_pct'], off['settle_s'], off['h2_disturbance_mm'],
             off['steady_err_h1'], off['steady_err_h2']][i]
        print(f"{name:<20}{str(a):>16}{str(b):>16}")
    print("=" * 62)

    if off["h2_disturbance_mm"] > 0:
        reduction = (1 - on["h2_disturbance_mm"] / off["h2_disturbance_mm"]) * 100
        print(f"解耦使 T2 受扰峰值降低 {reduction:.1f}%")

    summary = {
        "experiment": {
            "sp1_base": SP1_BASE, "sp2_base": SP2_BASE, "sp1_step": SP1_STEP,
            "settle_s": SETTLE_S, "observe_s": OBSERVE_S, "sample_s": SAMPLE_S,
        },
        "decouple_on": {k: v for k, v in on.items() if k != "samples"},
        "decouple_off": {k: v for k, v in off.items() if k != "samples"},
    }
    with open(os.path.join(DOCS, "step_response_realtime.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with open(os.path.join(DOCS, "step_response.csv"), "w", encoding="utf-8") as f:
        f.write("mode,t,h1,h2,h3,u1,u2\n")
        for key, data in (("on", on), ("off", off)):
            for s in data["samples"]:
                f.write(f"{key},{s['t']},{s['h1']},{s['h2']},{s['h3']},{s['u1']},{s['u2']}\n")

    print(f"\n已写入 docs/step_response_realtime.json 与 docs/step_response.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
