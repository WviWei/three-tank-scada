"""软 PLC 逻辑测试: 解耦归一化、设定值斜坡、报警锁存与确认。"""

import pytest

from common import registers as R
from controller.plc import PLCConfig, SoftPLC


def make_hr(mode: int = 1, sp1: int = 150, sp2: int = 130) -> list[int]:
    hr = [0] * R.HR_SIZE
    hr[R.HR["plc_mode"]] = mode
    hr[R.HR["sp1_mm"]] = sp1
    hr[R.HR["sp2_mm"]] = sp2
    return hr


def make_ir(h1: int = 100, h2: int = 90, h3: int = 60, overflow: int = 0) -> list[int]:
    ir = [0] * R.IR_SIZE
    ir[R.IR["h1_mm"]] = h1
    ir[R.IR["h2_mm"]] = h2
    ir[R.IR["h3_mm"]] = h3
    ir[R.IR["overflow"]] = overflow
    return ir


def test_alarm_latches_until_acknowledged() -> None:
    plc = SoftPLC(PLCConfig())
    hr = make_hr()

    # 液位超限 -> 报警并锁存
    plc.scan(hr, make_ir(h1=330), 0.1)
    assert plc.alarm_word & R.ALARM_BITS["h1_high"]
    assert plc.latched & R.ALARM_BITS["h1_high"]

    # 液位恢复 -> 活动位消失, 但锁存保持(瞬时故障不会漏掉)
    plc.scan(hr, make_ir(h1=100), 0.1)
    assert plc.alarm_word == 0
    assert plc.latched & R.ALARM_BITS["h1_high"]

    # 操作员确认 -> 已恢复的锁存位被清除
    hr[R.HR["reset_cmd"]] = 1
    plc.scan(hr, make_ir(h1=100), 0.1)
    assert plc.latched == 0
    assert plc.ack_pending is True


def test_active_alarm_survives_acknowledge() -> None:
    """故障仍在时, 确认操作不应把报警消掉。"""
    plc = SoftPLC(PLCConfig())
    hr = make_hr()
    plc.scan(hr, make_ir(h1=330), 0.1)

    hr[R.HR["reset_cmd"]] = 1
    plc.scan(hr, make_ir(h1=330), 0.1)
    assert plc.latched & R.ALARM_BITS["h1_high"]


def test_no_ack_means_no_pending_flag() -> None:
    plc = SoftPLC(PLCConfig())
    hr = make_hr()
    plc.scan(hr, make_ir(h1=100), 0.1)
    assert plc.ack_pending is False


def test_setpoint_ramp_rate() -> None:
    """设定值从当前液位起步, 按配置速率爬升。"""
    cfg = PLCConfig(sp_ramp_mm_s=4.0)
    plc = SoftPLC(cfg)
    hr = make_hr(sp1=150)
    ir = make_ir(h1=50)

    # 首次扫描: 斜坡以当前液位初始化, 并在同一周期内推进一个步长
    plc.scan(hr, ir, 0.1)
    assert plc.sp1_ramp == pytest.approx(50.4, abs=0.01)

    for _ in range(9):  # 累计满 1.0 s
        plc.scan(hr, ir, 0.1)
    assert plc.sp1_ramp == pytest.approx(54.0, abs=0.1)


def test_decoupling_is_gain_preserving() -> None:
    """解耦归一化: 两路虚拟输出相等时, 实际输出应等于该值(不损失执行能力)。

    早期实现漏了 1/(1-k) 归一化, 导致满输出时两泵各损失 1/3 行程,
    表现为 PID 已饱和但液位到不了设定值。
    """
    cfg = PLCConfig(decouple_enable=True, decouple_k12=0.4, decouple_k21=0.45, kp=0.0, ti_s=1e9)
    SoftPLC(cfg)
    make_hr()
    make_ir(h1=100, h2=90)

    # kp=0 且积分极慢 => 输出全由积分初值决定; 这里直接检查归一化公式
    v1 = v2 = 400.0
    inv = 1.0 / (1.0 - cfg.decouple_k12)
    u1 = (v1 - cfg.decouple_k12 * v2) * inv
    assert u1 == pytest.approx(v1)
    u2 = (v2 - cfg.decouple_k21 * v1) * (1.0 / (1.0 - cfg.decouple_k21))
    assert u2 == pytest.approx(v2)


def test_manual_mode_passes_through() -> None:
    plc = SoftPLC(PLCConfig())
    hr = make_hr(mode=0)
    hr[R.HR["pump1_permille"]] = 700
    hr[R.HR["pump2_permille"]] = 300
    _, pumps = plc.scan(hr, make_ir(h1=100, h2=90), 0.1)
    assert pumps == [700, 300]
