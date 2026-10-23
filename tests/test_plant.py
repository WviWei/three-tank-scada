"""被控对象模型的物理一致性测试。

用解析稳态关系核对数值积分, 可以快速发现方程写错、
单位换算错误、子步长不足等问题。
"""

import math

import pytest

from plant.plant import PlantParams, ThreeTankPlant


def run_steady(pump1: int = 500, pump2: int = 450, seconds: float = 260.0) -> ThreeTankPlant:
    p = ThreeTankPlant(PlantParams())
    p.set_pump_command(pump1 / 1000.0, pump2 / 1000.0)
    steps = int(seconds / 0.05)
    for _ in range(steps):
        for _ in range(10):
            p.integrate(0.005)
    return p


def test_steady_state_matches_analytic_solution() -> None:
    """稳态解: h3=((q1+q2)/c3)^2, h1=h3+(q1/c13)^2, h2=h3+(q2/c23)^2"""
    p = run_steady()
    q1 = p.u_cmd[0] * p.p.pump_max_flow
    q2 = p.u_cmd[1] * p.p.pump_max_flow
    h3 = ((q1 + q2) / p.p.c3) ** 2
    h1 = h3 + (q1 / p.p.c13) ** 2
    h2 = h3 + (q2 / p.p.c23) ** 2

    assert p.h[2] == pytest.approx(h3, abs=0.01)
    assert p.h[0] == pytest.approx(h1, abs=0.01)
    assert p.h[1] == pytest.approx(h2, abs=0.01)


def test_mass_balance_at_steady_state() -> None:
    p = run_steady()
    inflow = p.q_pump[0] + p.q_pump[1]
    assert p.q3 == pytest.approx(inflow, rel=0.02)


def test_level_is_clamped_to_tank_height() -> None:
    p = ThreeTankPlant(PlantParams(h1_init=10.0, h2_init=10.0, h3_init=10.0))
    assert p.h[0] <= p.p.height
    assert p.h[1] <= p.p.height
    assert p.h[2] <= p.p.height


def test_pump_command_is_clamped() -> None:
    p = ThreeTankPlant(PlantParams())
    p.set_pump_command(5.0, -2.0)
    assert p.u_cmd == [1.0, 0.0]


def test_overflow_flag_sets_when_level_exceeds_height() -> None:
    p = ThreeTankPlant(PlantParams(h1_init=0.0, h2_init=0.0, h3_init=0.0))
    p.set_pump_command(1.0, 1.0)
    for _ in range(20000):  # 1000 s 仿真, 必然充满
        p.integrate(0.005)
    assert p.overflow_mask != 0
    assert math.isclose(p.h[0], p.p.height, abs_tol=1e-6)
