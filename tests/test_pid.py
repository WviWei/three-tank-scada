"""PID 控制器单元测试, 重点验证抗积分饱和与无扰切换。

这两个特性在实机上出问题的表现是"切换模式时执行器猛冲"和
"长时间饱和后回不来", 靠肉眼很难在生产环境中复现。
"""

from controller.plc import PID, PLCConfig


def make_pid() -> PID:
    return PID(PLCConfig(), 0, 1000)


def test_output_is_limited() -> None:
    pid = make_pid()
    assert pid.update(1000, 0, 0.1) == 1000
    assert pid.update(0, 1000, 0.1) == 0


def test_integral_anti_windup() -> None:
    """长时间饱和后, 积分项不应累积到无法回退的程度。"""
    pid = make_pid()
    for _ in range(300):
        pid.update(1000, 0, 0.1)  # 持续大正偏差, 输出顶在 1000
    assert pid.out == 1000

    # 偏差反向, 应在有限步数内退出饱和
    for _ in range(30):
        out = pid.update(0, 1000, 0.1)
    assert out < 1000, "抗饱和失效: 输出无法从上限退出"


def test_bumpless_transfer_holds_output() -> None:
    """MAN->AUTO 切换瞬间, 偏差为零时输出应保持原手动值。"""
    pid = make_pid()
    pid.reset(500, 150, 150)
    out = pid.update(150, 150, 0.1)
    assert abs(out - 500) < 1.0


def test_zero_dt_is_safe() -> None:
    pid = make_pid()
    pid.update(100, 50, 0.1)
    before = pid.out
    assert pid.update(100, 50, 0.0) == before
