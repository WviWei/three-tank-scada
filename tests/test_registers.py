"""寄存器编解码与工程量换算的单元测试。

这一层是所有模块共用的契约, 一旦缩放系数或符号处理出错,
表现为"界面数值对但控制行为怪", 极难排查, 因此必须测死。
"""

from common import registers as R


def test_level_roundtrip() -> None:
    assert R.m_to_mm(0.15) == 150
    assert R.m_to_mm(0.3005) == 300  # 四舍五入到 1 mm
    assert R.mm_to_m(150) == 0.15


def test_flow_scaling() -> None:
    # 1e-4 m3/s = 6 L/min = 600 (L/min x100)
    assert R.m3s_to_reg(1e-4) == 600
    assert abs(R.reg_to_m3s(600) - 1e-4) < 1e-12


def test_signed16_encoding() -> None:
    assert R.to_signed16(-5) == 0xFFFB
    assert R.from_signed16(0xFFFB) == -5
    assert R.from_signed16(5) == 5
    assert R.from_signed16(0) == 0


def test_clamping() -> None:
    assert R.m_to_mm(-1.0) == 0
    assert R.m_to_mm(100.0) == 0xFFFF
    assert R.m3s_to_reg(-1.0) == 0


def test_register_layout_is_unique() -> None:
    ir_values = list(R.IR.values())
    hr_values = list(R.HR.values())
    assert len(ir_values) == len(set(ir_values)), "IR 地址有重复"
    assert len(hr_values) == len(set(hr_values)), "HR 地址有重复"
    assert all(0 <= v < R.IR_SIZE for v in ir_values)
    assert all(0 <= v < R.HR_SIZE for v in hr_values)
