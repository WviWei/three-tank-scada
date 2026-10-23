"""对象辨识与解耦效果评估(离线数值仿真)。

为什么用离线仿真而不是实时链路?
--------------------------------
控制算法设计与参数整定需要反复迭代。实时链路一次实验约需 3 分钟,
而本脚本直接复用同一套 ThreeTankPlant / SoftPLC 代码做前向仿真,
几秒即可完成, 且不受调度抖动影响。参数定稿后再用 step_test.py
在真实 Modbus 链路上复核。

产出
----
1. 开环稳态增益矩阵 G 与相对增益阵 RGA —— 定量说明耦合强弱;
2. 解耦 ON/OFF 的闭环阶跃响应对比 —— 定量说明解耦收益;
3. docs/step_response.json / .csv
"""

from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import registers as R  # noqa: E402
from controller.plc import PLCConfig, SoftPLC  # noqa: E402
from plant.plant import PlantParams, ThreeTankPlant  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

PLANT_DT = 0.05
PLC_DT = 0.1
SUBSTEPS = 10


def new_state(mode: int = 0, sp1: int = 150, sp2: int = 130) -> tuple:
    plant = ThreeTankPlant(PlantParams())
    hr = [0] * R.HR_SIZE
    hr[R.HR["pump1_permille"]] = 500
    hr[R.HR["pump2_permille"]] = 450
    hr[R.HR["sp1_mm"]] = sp1
    hr[R.HR["sp2_mm"]] = sp2
    hr[R.HR["plc_mode"]] = mode
    return plant, hr


def apply_commands(plant: ThreeTankPlant, hr: list[int]) -> None:
    """把寄存器里的泵指令下发给对象 —— 对应实时链路中 plant 每周期读 HR 的动作。"""
    plant.set_pump_command(
        hr[R.HR["pump1_permille"]] / 1000.0,
        hr[R.HR["pump2_permille"]] / 1000.0,
    )


def step_plant(plant: ThreeTankPlant) -> None:
    for _ in range(SUBSTEPS):
        plant.integrate(PLANT_DT / SUBSTEPS)


def plc_tick(plc: SoftPLC, hr: list[int], ir: list[int]) -> None:
    status, pumps = plc.scan(hr, ir, PLC_DT)
    base = R.HR["plc_heartbeat"]
    for offset, value in enumerate(status):
        hr[base + offset] = value
    if hr[R.HR["plc_mode"]] == 1:
        hr[R.HR["pump1_permille"]] = pumps[0]
        hr[R.HR["pump2_permille"]] = pumps[1]


# ------------------------------------------------------------------ 开环辨识
def steady_levels(pump1: int, pump2: int, seconds: float = 220.0) -> tuple[float, float]:
    plant, hr = new_state()
    hr[R.HR["pump1_permille"]] = pump1
    hr[R.HR["pump2_permille"]] = pump2
    apply_commands(plant, hr)
    n = int(seconds / PLANT_DT)
    tail = max(1, int(20 / PLANT_DT))
    acc1: list[float] = []
    acc2: list[float] = []
    for i in range(n):
        step_plant(plant)
        if i >= n - tail:
            acc1.append(plant.h[0] * 1000)
            acc2.append(plant.h[1] * 1000)
    return statistics.fmean(acc1), statistics.fmean(acc2)


def identify() -> dict:
    print("开环稳态增益辨识 ...")
    u1_0, u2_0 = 500, 450
    h1_00, h2_00 = steady_levels(u1_0, u2_0)
    print(f"  基准工作点: u1={u1_0} u2={u2_0} -> h1={h1_00:.1f} h2={h2_00:.1f} mm")

    u1_1 = 700
    h1_10, h2_10 = steady_levels(u1_1, u2_0)
    g11 = (h1_10 - h1_00) / (u1_1 - u1_0)
    g21 = (h2_10 - h2_00) / (u1_1 - u1_0)
    print(f"  泵1 阶跃 {u1_0}->{u1_1}: h1={h1_10:.1f} h2={h2_10:.1f}")
    print(f"    g11={g11:.4f} mm/permille  g21={g21:.4f} mm/permille")

    u2_1 = 650
    h1_01, h2_01 = steady_levels(u1_0, u2_1)
    g12 = (h1_01 - h1_00) / (u2_1 - u2_0)
    g22 = (h2_01 - h2_00) / (u2_1 - u2_0)
    print(f"  泵2 阶跃 {u2_0}->{u2_1}: h1={h1_01:.1f} h2={h2_01:.1f}")
    print(f"    g12={g12:.4f} mm/permille  g22={g22:.4f} mm/permille")

    det = g11 * g22 - g12 * g21
    rga11 = g11 * g22 / det
    rga12 = -g12 * g21 / det
    coupling_index = abs(g12 * g21) / abs(g11 * g22)

    print(f"  RGA = [[{rga11:.3f}, {rga12:.3f}], ...]  (理想配对时对角元应为 1)")
    print(f"  耦合度 |g12*g21|/|g11*g22| = {coupling_index:.3f}")
    return {
        "operating_point": {"u1": u1_0, "u2": u2_0, "h1": round(h1_00, 1), "h2": round(h2_00, 1)},
        "gain_matrix": {
            "g11": round(g11, 4), "g12": round(g12, 4),
            "g21": round(g21, 4), "g22": round(g22, 4),
        },
        "rga": {"lambda11": round(rga11, 4), "lambda12": round(rga12, 4)},
        "coupling_index": round(coupling_index, 4),
        "ideal_decouple_k": {
            "k12": round(-g12 / g11, 4),
            "k21": round(-g21 / g22, 4),
        },
    }


# ------------------------------------------------------------------ 闭环仿真
def closed_loop(decouple: bool, total_s: float = 340.0) -> dict:
    cfg = PLCConfig(decouple_enable=decouple)
    plc = SoftPLC(cfg)
    plant, hr = new_state(mode=0)
    sp1_base, sp2_base = 150, 130
    sp1_step = 175
    t_auto, t_step = 60.0, 210.0
    obs_s = total_s - t_step

    dt = PLANT_DT
    records: list[dict] = []
    i = 0
    while True:
        t = i * dt
        if t >= total_s:
            break
        if t >= t_auto:
            hr[R.HR["plc_mode"]] = 1
        if t >= t_step:
            hr[R.HR["sp1_mm"]] = sp1_step

        ir = plant.snapshot_ir()
        if i % 2 == 0:
            plc_tick(plc, hr, ir)

        if i % 20 == 0:
            records.append({
                "t": round(t, 1),
                "h1": ir[R.IR["h1_mm"]],
                "h2": ir[R.IR["h2_mm"]],
                "h3": ir[R.IR["h3_mm"]],
                "u1": hr[R.HR["pump1_permille"]],
                "u2": hr[R.HR["pump2_permille"]],
                "sp1": hr[R.HR["sp1_mm"]],
                "sp2": hr[R.HR["sp2_mm"]],
            })

        apply_commands(plant, hr)
        step_plant(plant)
        i += 1

    pre = [r for r in records if t_step - 20 <= r["t"] < t_step]
    post = [r for r in records if r["t"] >= t_step]
    h1_base = statistics.fmean(r["h1"] for r in pre)
    h2_base = statistics.fmean(r["h2"] for r in pre)

    peak = max(r["h1"] for r in post)
    amp = sp1_step - h1_base
    overshoot = max(0.0, (peak - sp1_step) / amp * 100.0)

    band = 0.02 * amp
    settle = None
    for k in range(len(post)):
        if all(abs(r["h1"] - sp1_step) <= band for r in post[k:]):
            settle = round(post[k]["t"] - t_step, 1)
            break

    h2_dev = max(abs(r["h2"] - h2_base) for r in post)
    u2_swing = max(r["u2"] for r in post) - min(r["u2"] for r in post)

    return {
        "decouple": decouple,
        "h1_base": round(h1_base, 1),
        "h2_base": round(h2_base, 1),
        "sp1_step": sp1_step,
        "sp2": sp2_base,
        "peak_h1": peak,
        "overshoot_pct": round(overshoot, 2),
        "settle_s": settle,
        "h2_disturbance_mm": round(h2_dev, 2),
        "u2_swing": u2_swing,
        "final_h1": post[-1]["h1"],
        "final_h2": post[-1]["h2"],
        "records": records,
    }


def main() -> int:
    os.makedirs(DOCS, exist_ok=True)

    ident = identify()

    print("\n闭环阶跃对比仿真 ...")
    on = closed_loop(True)
    print("  解耦 ON  完成")
    off = closed_loop(False)
    print("  解耦 OFF 完成")

    print("\n" + "=" * 64)
    print(f"{'指标':<22}{'解耦 ON':>18}{'解耦 OFF':>18}")
    print("-" * 64)
    rows = [
        ("T1 超调量 (%)", on["overshoot_pct"], off["overshoot_pct"]),
        ("T1 调节时间 (s)", on["settle_s"], off["settle_s"]),
        ("T2 受扰峰值 (mm)", on["h2_disturbance_mm"], off["h2_disturbance_mm"]),
        ("P2 输出摆幅 (permille)", on["u2_swing"], off["u2_swing"]),
        ("T1 稳态误差 (mm)", on["final_h1"] - 175, off["final_h1"] - 175),
        ("T2 稳态误差 (mm)", on["final_h2"] - 130, off["final_h2"] - 130),
    ]
    for name, a, b in rows:
        print(f"{name:<22}{str(a):>18}{str(b):>18}")
    print("=" * 64)

    if off["h2_disturbance_mm"] > 0:
        red = (1 - on["h2_disturbance_mm"] / off["h2_disturbance_mm"]) * 100
        print(f"解耦使 T2 受扰峰值降低 {red:.1f}%")
    if off["u2_swing"] > 0:
        red2 = (1 - on["u2_swing"] / off["u2_swing"]) * 100
        print(f"解耦使 P2 输出摆幅降低 {red2:.1f}%")

    summary = {
        "plant_identification": ident,
        "experiment": {"sp1_base": 150, "sp2_base": 130, "sp1_step": 175,
                       "auto_at_s": 60, "step_at_s": 210},
        "decouple_on": {k: v for k, v in on.items() if k != "records"},
        "decouple_off": {k: v for k, v in off.items() if k != "records"},
    }
    with open(os.path.join(DOCS, "identification.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    with open(os.path.join(DOCS, "step_response.csv"), "w", encoding="utf-8") as f:
        f.write("mode,t,h1,h2,h3,u1,u2,sp1,sp2\n")
        for key, data in (("on", on), ("off", off)):
            for r in data["records"]:
                f.write(f"{key},{r['t']},{r['h1']},{r['h2']},{r['h3']},"
                        f"{r['u1']},{r['u2']},{r['sp1']},{r['sp2']}\n")

    print("\n已写入 docs/identification.json 与 docs/step_response.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
