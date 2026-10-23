"""软 PLC: 双回路液位控制 + 静态解耦补偿。

架构定位
--------
本进程扮演真实系统里的 PLC(Master) 角色:
    Modbus Client --> 读 plant 的 PV 与操作员指令 --> 执行控制算法
    Modbus Client --> 把泵指令与状态写回 plant

扫描周期 100 ms, 远快于被控对象约 30 s 的主导时间常数,
因此控制律可以按连续系统设计。

控制难点与对策
--------------
T3 是两个回路的公共下游。q1 抬高 h3 后, T2 的下泄压差 (h2-h3) 减小,
于是 h2 被动升高 —— 这就是交叉耦合。若两回路各自独立整定,
现场表现为"整定好一个, 另一个就波动"。

稳态增益矩阵(在标称工作点线性化):
    G = [[a+b1, a], [a, a+b2]]
    a  = 2(q1+q2)/c3^2        公共下游项(耦合来源)
    b1 = 2*q1/c13^2,  b2 = 2*q2/c23^2

采用静态解耦器 D = [[1, d12], [d21, 1]] 使 G·D 对角:
    d12 = -a/(a+b1),  d21 = -a/(a+b2)
解耦后两个回路可独立整定, 阶跃响应互不影响。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, fields

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import AsyncModbusTcpClient  # noqa: E402

from common import registers as R  # noqa: E402

log = logging.getLogger("plc")


# ------------------------------------------------------------------ 配置
@dataclass
class PLCConfig:
    scan_s: float = 0.1              # 扫描周期 s
    kp: float = 6.0                  # 比例增益 permille/mm
    ti_s: float = 12.0               # 积分时间 s
    td_s: float = 0.0                # 微分时间 s (液位对象一阶惯性, 一般不需要 D)
    sp_ramp_mm_s: float = 4.0        # 设定值爬坡速率, 抑制大偏差下的超调
    # 静态解耦系数。理论值 d12=-a/(a+b1), d21=-a/(a+b2);
    # 标称工作点下约为 0.68 / 0.76, 这里取 0.6 倍作"部分解耦":
    # 全解耦在设定值大阶跃时会把执行器推到饱和, 反而恶化动态品质。
    decouple_k12: float = 0.40
    decouple_k21: float = 0.45
    decouple_enable: bool = True
    h_high_mm: int = 320             # 高液位报警阈值
    pump_min: int = 0
    pump_max: int = 1000


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ------------------------------------------------------------------ PID
class PID:
    """位置式 PID, 带积分抗饱和(clamping)与无扰切换。"""

    def __init__(self, cfg: PLCConfig, out_min: int, out_max: int) -> None:
        self.cfg = cfg
        self.out_min = out_min
        self.out_max = out_max
        self.integral = 0.0
        self.prev_err = 0.0
        self.out = 0.0

    def reset(self, out: float, sp: float, pv: float) -> None:
        """无扰切换: 令积分项恰好复现当前输出, 避免 MAN->AUTO 时输出跳变。"""
        self.out = clamp(out, self.out_min, self.out_max)
        self.integral = self.out - self.cfg.kp * (sp - pv)
        self.prev_err = sp - pv

    def update(self, sp: float, pv: float, dt: float) -> float:
        if dt <= 0:
            return self.out
        err = sp - pv
        p_term = self.cfg.kp * err
        i_candidate = self.integral + self.cfg.kp / self.cfg.ti_s * err * dt
        d_term = 0.0
        if self.cfg.td_s > 0:
            d_term = self.cfg.kp * self.cfg.td_s * (err - self.prev_err) / dt

        raw = p_term + i_candidate + d_term
        out = clamp(raw, self.out_min, self.out_max)

        # 抗饱和: 输出未饱和, 或积分方向有利于退出饱和时才累积
        saturated = raw != out
        pushing_further = (raw > out and err > 0) or (raw < out and err < 0)
        if not (saturated and pushing_further):
            self.integral = i_candidate

        self.out = out
        self.prev_err = err
        return out


# ------------------------------------------------------------------ PLC
class SoftPLC:
    def __init__(self, cfg: PLCConfig) -> None:
        self.cfg = cfg
        self.pid1 = PID(cfg, cfg.pump_min, cfg.pump_max)
        self.pid2 = PID(cfg, cfg.pump_min, cfg.pump_max)
        self.sp1_ramp = 0.0
        self.sp2_ramp = 0.0
        self.prev_mode = 0
        self.initialized = False
        self.heartbeat = 0
        self.alarm_word = 0
        self.scan_ms = int(cfg.scan_s * 1000)
        self.decouple_term = 0

    # -------------------------------------------------------------- 设定值斜坡
    @staticmethod
    def _ramp(current: float, target: float, rate: float, dt: float) -> float:
        step = rate * dt
        if abs(target - current) <= step:
            return target
        return current + step if target > current else current - step

    # -------------------------------------------------------------- 一次扫描
    def scan(self, hr: list[int], ir: list[int], dt: float) -> tuple[list[int], list[int]]:
        """执行一个扫描周期, 返回 (要写入 HR 的状态区, 报警新状态)。"""
        cfg = self.cfg

        h1 = ir[R.IR["h1_mm"]]
        h2 = ir[R.IR["h2_mm"]]
        h3 = ir[R.IR["h3_mm"]]

        # 首次扫描时用当前液位初始化斜坡, 保证 MAN->AUTO 切换时偏差为零
        if not self.initialized:
            self.sp1_ramp = float(h1)
            self.sp2_ramp = float(h2)
            self.initialized = True

        mode = hr[R.HR["plc_mode"]]
        sp1 = hr[R.HR["sp1_mm"]]
        sp2 = hr[R.HR["sp2_mm"]]
        manual1 = hr[R.HR["pump1_permille"]]
        manual2 = hr[R.HR["pump2_permille"]]

        # 设定值下限保护: 0 mm 无意义, 且会让积分长期负饱和
        sp1 = max(20, min(R.H_FULL_MM - 30, sp1))
        sp2 = max(20, min(R.H_FULL_MM - 30, sp2))

        if mode == 1:
            # 自动模式: 先爬坡再进 PID
            self.sp1_ramp = self._ramp(self.sp1_ramp, sp1, cfg.sp_ramp_mm_s, dt)
            self.sp2_ramp = self._ramp(self.sp2_ramp, sp2, cfg.sp_ramp_mm_s, dt)

            if self.prev_mode != 1:
                # MAN -> AUTO 无扰切换
                self.pid1.reset(manual1, self.sp1_ramp, h1)
                self.pid2.reset(manual2, self.sp2_ramp, h2)
                log.info("切入 AUTO, 无扰切换完成 (泵1=%.1f%% 泵2=%.1f%%)", manual1 / 10, manual2 / 10)

            v1 = self.pid1.update(self.sp1_ramp, h1, dt)
            v2 = self.pid2.update(self.sp2_ramp, h2, dt)

            # 静态解耦: u = D · v, D 需归一化到列和为 1
            #
            # 未归一化的 D = [[1, -k], [-k, 1]] 在满输出时会让两个泵各损失 k 的执行能力,
            # 表现为"PID 已经饱和但液位仍到不了设定值"。归一化因子 1/(1-k) 恰好补回。
            if cfg.decouple_enable:
                inv = 1.0 / (1.0 - cfg.decouple_k12)
                u1 = (v1 - cfg.decouple_k12 * v2) * inv
                u2 = (v2 - cfg.decouple_k21 * v1) * inv
                self.decouple_term = int(round(abs(cfg.decouple_k12 * v2) * inv)) & 0xFFFF
            else:
                u1, u2 = v1, v2
                self.decouple_term = 0

            out1 = int(round(clamp(u1, cfg.pump_min, cfg.pump_max)))
            out2 = int(round(clamp(u2, cfg.pump_min, cfg.pump_max)))
            err1 = int(round(self.sp1_ramp - h1))
            err2 = int(round(self.sp2_ramp - h2))
        else:
            # 手动模式: 直接透传操作员指令; 斜坡跟踪实际液位而非设定值,
            # 这样切回 AUTO 时偏差为零, 输出原地保持, 即无扰切换。
            out1 = manual1
            out2 = manual2
            self.sp1_ramp = float(h1)
            self.sp2_ramp = float(h2)
            err1 = int(round(sp1 - h1))
            err2 = int(round(sp2 - h2))
            self.decouple_term = 0

        self.prev_mode = mode

        # ---------------------------------------------------------- 报警
        alarm = 0
        if h1 >= cfg.h_high_mm:
            alarm |= R.ALARM_BITS["h1_high"]
        if h2 >= cfg.h_high_mm:
            alarm |= R.ALARM_BITS["h2_high"]
        if h3 >= cfg.h_high_mm:
            alarm |= R.ALARM_BITS["h3_high"]
        overflow = ir[R.IR["overflow"]]
        if overflow & 0b001:
            alarm |= R.ALARM_BITS["h1_overflow"]
        if overflow & 0b010:
            alarm |= R.ALARM_BITS["h2_overflow"]
        if overflow & 0b100:
            alarm |= R.ALARM_BITS["h3_overflow"]
        self.alarm_word = alarm

        self.heartbeat = (self.heartbeat + 1) & 0xFFFF

        status = [0] * (R.HR_SIZE - R.HR["plc_heartbeat"])
        base = R.HR["plc_heartbeat"]
        status[R.HR["plc_heartbeat"] - base] = self.heartbeat
        status[R.HR["plc_alarm_word"] - base] = alarm
        status[R.HR["pid1_out"] - base] = out1
        status[R.HR["pid2_out"] - base] = out2
        status[R.HR["pid1_err_mm"] - base] = R.to_signed16(err1)
        status[R.HR["pid2_err_mm"] - base] = R.to_signed16(err2)
        status[R.HR["decouple_term"] - base] = self.decouple_term
        status[R.HR["plc_scan_ms"] - base] = self.scan_ms

        return status, [out1, out2]


# ------------------------------------------------------------------ 主循环
async def plc_loop(cfg: PLCConfig, host: str, port: int) -> None:
    plc = SoftPLC(cfg)
    client = AsyncModbusTcpClient(host, port=port, timeout=1.0)
    connected = False
    last_scan = time.perf_counter()
    comm_fail = 0

    while True:
        tick = time.perf_counter()

        if not connected:
            connected = await client.connect()
            if connected:
                log.info("已连接现场从站 %s:%d", host, port)
                comm_fail = 0
            else:
                await asyncio.sleep(1.0)
                continue

        try:
            hr_rr = await client.read_holding_registers(0, count=R.HR_SIZE, slave=0)
            ir_rr = await client.read_input_registers(0, count=R.IR_SIZE, slave=0)
            if hr_rr.isError() or ir_rr.isError():
                raise IOError(f"读取异常 hr={hr_rr} ir={ir_rr}")

            dt = time.perf_counter() - last_scan
            dt = clamp(dt, 0.0, 1.0)
            status, pumps = plc.scan(hr_rr.registers, ir_rr.registers, dt)
            last_scan = time.perf_counter()

            wr = await client.write_registers(R.HR["plc_heartbeat"], status, slave=0)
            if wr.isError():
                raise IOError(f"写入异常 {wr}")

            # 泵指令是控制输出, 只在 AUTO 下由 PLC 写
            if hr_rr.registers[R.HR["plc_mode"]] == 1:
                await client.write_register(R.HR["pump1_permille"], pumps[0], slave=0)
                await client.write_register(R.HR["pump2_permille"], pumps[1], slave=0)

            comm_fail = 0
        except Exception as exc:  # noqa: BLE001 - 现场通讯任何异常都要能自恢复
            comm_fail += 1
            log.warning("通讯异常(%d): %s", comm_fail, exc)
            if comm_fail >= 3:
                client.close()
                connected = False
            await asyncio.sleep(0.5)
            continue

        elapsed = time.perf_counter() - tick
        plc.scan_ms = int(elapsed * 1000)
        await asyncio.sleep(max(0.0, cfg.scan_s - elapsed))


def main() -> None:
    p = argparse.ArgumentParser(description="软 PLC: 双回路液位控制 + 静态解耦")
    p.add_argument("--host", default=R.PLANT_HOST)
    p.add_argument("--port", type=int, default=R.PLANT_PORT)
    p.add_argument("--config", default="", help="覆盖配置的 JSON")
    p.add_argument("--no-decouple", action="store_true", help="关闭解耦补偿(用于对比实验)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = PLCConfig()
    if args.config:
        override = json.loads(args.config)
        valid = {f.name for f in fields(PLCConfig)}
        for k, v in override.items():
            if k in valid:
                setattr(cfg, k, type(getattr(cfg, k))(v))
            else:
                log.warning("未知配置项 %s, 已忽略", k)
    if args.no_decouple:
        cfg.decouple_enable = False

    log.info("软 PLC 启动 | %s", json.dumps(asdict(cfg), ensure_ascii=False))
    try:
        asyncio.run(plc_loop(cfg, args.host, args.port))
    except KeyboardInterrupt:
        log.info("PLC 已停止")


if __name__ == "__main__":
    main()
