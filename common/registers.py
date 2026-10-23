"""Modbus 寄存器映射与工程量换算。

现场设备(plant) / 软 PLC(controller) / 上位机(scada) 三方共用此契约，
任何一方改动寄存器布局都必须同步本文件，避免地址漂移导致的隐性故障。

约定:
  * Modbus 地址为 0 基（PDU 地址），文档中的 40001 对应 offset 0。
  * 所有寄存器为 16 位无符号整数，浮点量按定点缩放传输。
  * Input Register (FC04) 只读：现场实时值。
  * Holding Register (FC03/06/16) 读写：控制指令与状态回读。
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------- 网络拓扑
#: 通过环境变量覆盖, 便于 Docker Compose 中指向服务名
PLANT_HOST = os.environ.get("PLANT_HOST", "127.0.0.1")
PLANT_PORT = int(os.environ.get("PLANT_PORT", "5020"))

# ---------------------------------------------------------------- 工程量换算
#: 液位: 米 -> 毫米 (1 mm 分辨率)
H_SCALE = 1000.0
#: 流量: m3/s -> L/min x100 (0.01 L/min 分辨率, 满量程 6 L/min => 600)
Q_SCALE = 6_000_000.0
#: 泵指令: 百分比 -> 千分比 (0..1000)
P_SCALE = 1000.0

#: 水箱物理高度 (m)
TANK_HEIGHT_M = 0.35
#: 液位寄存器满量程
H_FULL_MM = int(TANK_HEIGHT_M * H_SCALE)

# ---------------------------------------------------------------- 数据点
IR: dict[str, int] = {
    "h1_mm": 0,          # 水箱 T1 液位
    "h2_mm": 1,          # 水箱 T2 液位
    "h3_mm": 2,          # 水箱 T3 液位(耦合通道)
    "q1_lpm100": 3,      # 泵1 实际流量
    "q2_lpm100": 4,      # 泵2 实际流量
    "q13_lpm100": 5,     # T1 -> T3 下泄流量
    "q23_lpm100": 6,     # T2 -> T3 下泄流量
    "q3_lpm100": 7,      # T3 排入地沟流量
    "overflow": 8,       # 溢流标志位(bit0..2 对应 T1..T3)
    "sim_time_s": 9,     # 仿真运行秒数(取整, 用于时间轴对齐)
}

HR: dict[str, int] = {
    # --- SCADA / 操作员写入 ---
    "pump1_permille": 0,     # 泵1 手动指令 0..1000
    "pump2_permille": 1,     # 泵2 手动指令 0..1000
    "plc_mode": 2,           # 0=手动(MAN) 1=自动(AUTO)
    "sp1_mm": 3,             # T1 液位设定值
    "sp2_mm": 4,             # T2 液位设定值
    "reset_cmd": 5,          # 写 1 复位报警(PLC 处理后清零)
    # --- 软 PLC 写入(状态回读) ---
    "plc_heartbeat": 6,      # 心跳计数, 递增证明 PLC 存活
    "plc_alarm_word": 7,     # 报警字(见 ALARM_BITS)
    "pid1_out": 8,           # 回路1 PID 输出(千分比)
    "pid2_out": 9,           # 回路2 PID 输出(千分比)
    "pid1_err_mm": 10,       # 回路1 偏差, 带符号(补码)
    "pid2_err_mm": 11,       # 回路2 偏差, 带符号(补码)
    "decouple_term": 12,     # 解耦补偿量(千分比, 绝对值)
    "plc_scan_ms": 13,       # PLC 实际扫描周期
    "alarm_latched": 14,     # 锁存报警字: 位一旦置位即保持, 直到操作员确认
}

#: 报警位定义
ALARM_BITS: dict[str, int] = {
    "h1_high": 1 << 0,
    "h2_high": 1 << 1,
    "h3_high": 1 << 2,
    "h1_overflow": 1 << 3,
    "h2_overflow": 1 << 4,
    "h3_overflow": 1 << 5,
    "plc_comm_lost": 1 << 6,
    "sensor_fault": 1 << 7,
}

ALARM_LABELS: dict[str, str] = {
    "h1_high": "T1 高液位",
    "h2_high": "T2 高液位",
    "h3_high": "T3 高液位",
    "h1_overflow": "T1 溢流",
    "h2_overflow": "T2 溢流",
    "h3_overflow": "T3 溢流",
    "plc_comm_lost": "PLC 通讯中断",
    "sensor_fault": "变送器故障",
}

#: 报警优先级 1=紧急 2=警告
ALARM_LEVEL: dict[str, int] = {
    "h1_high": 2,
    "h2_high": 2,
    "h3_high": 2,
    "h1_overflow": 1,
    "h2_overflow": 1,
    "h3_overflow": 1,
    "plc_comm_lost": 1,
    "sensor_fault": 1,
}

#: 寄存器区长度(带余量, 方便后续扩展)
IR_SIZE = 64
HR_SIZE = 64


def m_to_mm(h: float) -> int:
    return max(0, min(0xFFFF, int(round(h * H_SCALE))))


def mm_to_m(reg: int) -> float:
    return reg / H_SCALE


def m3s_to_reg(q: float) -> int:
    return max(0, min(0xFFFF, int(round(q * Q_SCALE))))


def reg_to_m3s(reg: int) -> float:
    return reg / Q_SCALE


def pct_to_permille(pct: float) -> int:
    return max(0, min(1000, int(round(pct * 10.0))))


def to_signed16(value: int) -> int:
    """把带符号量编码成 16 位补码。"""
    return value & 0xFFFF


def from_signed16(value: int) -> int:
    """把 16 位补码解码为带符号整数。"""
    return value - 0x10000 if value >= 0x8000 else value
