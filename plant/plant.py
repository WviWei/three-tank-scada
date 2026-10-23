"""三容水箱被控对象仿真 + Modbus TCP 从站。

物理结构
--------
        泵1 ----+--> T1 --\
                |         |  q13 = c13*sqrt(max(0, h1-h3))
        泵2 ----+--> T2 --/   q23 = c23*sqrt(max(0, h2-h3))
                          \\--> T3 --> q3 = c3*sqrt(h3)  (排入地沟)

T3 是两个回路的公共下游，因此 q1 抬高 h3 会反过来抑制 h2 的下泄，
这就是本系统交叉耦合的物理来源，也是解耦补偿要消除的对象。

稳态关系(用于控制器整定与解耦设计):
    h3 = ((q1 + q2) / c3)^2
    h1 = h3 + (q1 / c13)^2
    h2 = h3 + (q2 / c23)^2

本进程只做两件事: 按指令积分物理方程, 通过 Modbus 暴露实时值。
它不包含任何控制逻辑 —— 控制全部在 controller/plc.py 中。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, fields

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import registers as R  # noqa: E402  (需在 sys.path 调整之后导入)

log = logging.getLogger("plant")


@dataclass
class PlantParams:
    """对象参数, 全部为 SI 单位。可通过 --params 传入 JSON 覆盖。"""

    area: float = 5.0e-3           # 水箱横截面积 m2 (直径约 80 mm 立管)
    height: float = R.TANK_HEIGHT_M  # 水箱高度 m
    c13: float = 4.5e-4            # T1->T3 下泄系数
    c23: float = 4.5e-4            # T2->T3 下泄系数
    c3: float = 4.0e-4             # T3 排空系数 (偏小 => 公共下游影响大 => 耦合强)
    pump_max_flow: float = 1.5e-4  # 单泵最大流量 m3/s (9 L/min)
    pump_tau: float = 0.8          # 泵与管路一阶滞后时间常数 s
    h1_init: float = 0.050         # 初始液位 m (模拟装置开车前的存水)
    h2_init: float = 0.040
    h3_init: float = 0.030


class ThreeTankPlant:
    """三容水箱的数值积分模型。"""

    def __init__(self, params: PlantParams) -> None:
        self.p = params
        self.h = [
            min(params.h1_init, params.height),
            min(params.h2_init, params.height),
            min(params.h3_init, params.height),
        ]  # 液位 m
        self.u_cmd = [0.0, 0.0]            # 泵指令 0..1
        self.q_pump = [0.0, 0.0]           # 泵实际流量 m3/s
        self.q13 = 0.0
        self.q23 = 0.0
        self.q3 = 0.0
        self.overflow_mask = 0
        self.t = 0.0

    # ------------------------------------------------------------ 指令输入
    def set_pump_command(self, u1: float, u2: float) -> None:
        self.u_cmd = [
            min(1.0, max(0.0, u1)),
            min(1.0, max(0.0, u2)),
        ]

    # ------------------------------------------------------------ 物理方程
    def _flows(self, h1: float, h2: float, h3: float, q1: float, q2: float):
        # 下泄需要正压头, 反向时取 0 (单向阀)
        q13 = self.p.c13 * math.sqrt(max(0.0, h1 - h3))
        q23 = self.p.c23 * math.sqrt(max(0.0, h2 - h3))
        q3 = self.p.c3 * math.sqrt(max(0.0, h3))
        return q13, q23, q3

    def integrate(self, dt: float) -> None:
        """单步显式欧拉积分。dt 取 5 ms, 对本系统时间常数(~50 s)精度充足。"""
        h1, h2, h3 = self.h
        q1, q2 = self.q_pump
        a = self.p.area

        q13, q23, q3 = self._flows(h1, h2, h3, q1, q2)

        h1 += (q1 - q13) / a * dt
        h2 += (q2 - q23) / a * dt
        h3 += (q13 + q23 - q3) / a * dt

        # 泵一阶滞后逼近指令
        alpha = dt / self.p.pump_tau if self.p.pump_tau > 0 else 1.0
        alpha = min(1.0, alpha)
        for i in range(2):
            target = self.u_cmd[i] * self.p.pump_max_flow
            self.q_pump[i] += (target - self.q_pump[i]) * alpha

        # 液位限幅与溢流检测
        mask = 0
        for i, h in enumerate((h1, h2, h3)):
            if h > self.p.height:
                mask |= (1 << i)
                h = self.p.height
            elif h < 0.0:
                h = 0.0
            self.h[i] = h
        self.overflow_mask = mask

        self.q13, self.q23, self.q3 = q13, q23, q3
        self.t += dt

    # ------------------------------------------------------------ 快照
    def snapshot_ir(self) -> list[int]:
        ir = [0] * R.IR_SIZE
        ir[R.IR["h1_mm"]] = R.m_to_mm(self.h[0])
        ir[R.IR["h2_mm"]] = R.m_to_mm(self.h[1])
        ir[R.IR["h3_mm"]] = R.m_to_mm(self.h[2])
        ir[R.IR["q1_lpm100"]] = R.m3s_to_reg(self.q_pump[0])
        ir[R.IR["q2_lpm100"]] = R.m3s_to_reg(self.q_pump[1])
        ir[R.IR["q13_lpm100"]] = R.m3s_to_reg(self.q13)
        ir[R.IR["q23_lpm100"]] = R.m3s_to_reg(self.q23)
        ir[R.IR["q3_lpm100"]] = R.m3s_to_reg(self.q3)
        ir[R.IR["overflow"]] = self.overflow_mask
        ir[R.IR["sim_time_s"]] = int(self.t) & 0xFFFF
        return ir


# ------------------------------------------------------------------ Modbus
#: 上电默认指令: 模拟装置上次停车时的状态(两泵半开, MAN 模式)
DEFAULT_HR: dict[str, int] = {
    "pump1_permille": 350,
    "pump2_permille": 320,
    "plc_mode": 0,
    "sp1_mm": 150,
    "sp2_mm": 130,
}


def build_datastore() -> ModbusSlaveContext:
    """建立从站数据区。zero_mode=True 使寄存器偏移与文档中的 0 基地址一致。"""
    hr_init = [0] * R.HR_SIZE
    for name, value in DEFAULT_HR.items():
        hr_init[R.HR[name]] = value
    return ModbusSlaveContext(
        ir=ModbusSequentialDataBlock(0, [0] * R.IR_SIZE),
        hr=ModbusSequentialDataBlock(0, hr_init),
        co=ModbusSequentialDataBlock(0, [0] * 8),
        di=ModbusSequentialDataBlock(0, [0] * 8),
        zero_mode=True,
    )


async def simulation_loop(
    plant: ThreeTankPlant,
    store: ModbusSlaveContext,
    dt: float = 0.05,
) -> None:
    """仿真主循环: 固定 50 ms 周期, 内部 5 ms 子步。"""
    n_sub = max(1, int(round(dt / 0.005)))
    h_dt = dt / n_sub
    lag_log = 0.0

    while True:
        started = time.perf_counter()

        hr = store.getValues(3, 0, count=R.HR_SIZE)
        u1 = hr[R.HR["pump1_permille"]] / 1000.0
        u2 = hr[R.HR["pump2_permille"]] / 1000.0
        plant.set_pump_command(u1, u2)

        for _ in range(n_sub):
            plant.integrate(h_dt)

        store.setValues(4, 0, plant.snapshot_ir())

        # 周期补偿: 若积分耗时超出预算则缩短休眠, 保证平均步长不漂移
        elapsed = time.perf_counter() - started
        lag_log += elapsed
        if elapsed > dt:
            log.warning("仿真落后 %.1f ms, 单周期耗时 %.1f ms", (elapsed - dt) * 1e3, elapsed * 1e3)
        await asyncio.sleep(max(0.0, dt - elapsed))


async def main_async(args: argparse.Namespace) -> None:
    params = PlantParams()
    if args.params:
        override = json.loads(args.params)
        valid = {f.name for f in fields(PlantParams)}
        for k, v in override.items():
            if k in valid:
                setattr(params, k, float(v))
            else:
                log.warning("未知对象参数 %s, 已忽略", k)
        log.info("对象参数覆盖: %s", override)

    store = build_datastore()
    context = ModbusServerContext(slaves=store, single=True)
    plant = ThreeTankPlant(params)

    log.info(
        "三容水箱对象启动 | 面积=%.4f m2 高度=%.0f mm 泵上限=%.1f L/min",
        params.area, params.height * 1e3, params.pump_max_flow * 6e4,
    )
    log.info("Modbus TCP 从站监听 %s:%d", args.host, args.port)

    await asyncio.gather(
        StartAsyncTcpServer(context=context, address=(args.host, args.port)),
        simulation_loop(plant, store, dt=args.dt),
    )


def main() -> None:
    p = argparse.ArgumentParser(description="三容水箱被控对象仿真 + Modbus 从站")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=R.PLANT_PORT)
    p.add_argument("--dt", type=float, default=0.05, help="仿真周期 s")
    p.add_argument("--params", default="", help='对象参数 JSON, 如 {"c3": 4.0e-4}')
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log.info("对象仿真已停止")


if __name__ == "__main__":
    main()
