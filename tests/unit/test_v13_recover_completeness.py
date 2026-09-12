"""V13 W7a 回归测试 —— 恢复链路必须**完整**。

**这里钉死的是一个真 bug**。`POST /api/emergency/recover` 此前只调 `kill_switch.disarm()`,
而 `RiskStateMachine.reset()` / `confirm_recovered()` / `SystemLifecycle.exit_safe_mode()`
在全代码库里**没有任何生产调用者**。后果:

    回撤 15% 触发 state_machine.kill() → 风险态永久停在 KILLED
    关键任务崩溃 → 生命周期永久停在 SAFE_MODE
    → 两者都**只能靠重启进程脱身**(状态机是内存态, 重启回初值)

Pi 上卡在 SAFE_MODE + KILLED 就是这个 bug 的现场证据。

本文件用**真实**的 RiskManager / SystemLifecycle / TradingGate(不是桩)走完整链路,
断言「冻结之后一定能通过恢复流程回到可交易」。
"""

from __future__ import annotations

import pytest

from at01_common.operator_narrative import (
    KILL_ORIGIN_AUTO_EQUITY,
    KILL_ORIGIN_MANUAL,
)
from at50_risk.recovery_flow import assess_preconditions, perform_recovery
from at50_risk.risk_killswitch import KillSwitch
from at50_risk.risk_manager import RiskManager
from at50_risk.system_lifecycle import LifecycleState, SystemLifecycle
from at50_risk.trading_gate import TradingGate


@pytest.fixture
def system():
    """真实的风控 + 生命周期 + 闸门(不用桩 —— 桩会掩盖真正的连线问题)。"""
    rm = RiskManager()
    lifecycle = SystemLifecycle()
    gate = TradingGate(rm, lifecycle)
    return rm, lifecycle, gate


def _to_trading(lifecycle: SystemLifecycle) -> None:
    lifecycle.warm_up()
    lifecycle.sync()
    lifecycle.self_check()
    lifecycle.ready()
    lifecycle.start_trading()


def _freeze(system, *, origin: str = KILL_ORIGIN_MANUAL) -> None:
    """模拟一次冻结: 急停 armed + 风险 KILLED + 生命周期 SAFE_MODE。"""
    rm, lifecycle, _gate = system
    rm.kill_switch.arm("回撤 15% 触发急停", origin=origin)
    rm.state_machine.kill("最大回撤 15%")
    lifecycle.enter_safe_mode("回撤 15% 触发急停")


# ---------------------------------------------------------------------------
# 前置条件
# ---------------------------------------------------------------------------


def test_preconditions_require_a_trusted_account_state(system) -> None:
    """账户状态不可信时不得解冻 —— 否则等于拿着错误的账本去下真钱单。"""
    _rm, _lc, gate = system
    gate.reconciled = False
    check = assess_preconditions(gate, system[0])
    assert check.ok is False
    # V15: `missing` 现在给人看的**标签**(页面直接显示), 不再是内部常量串
    assert "对账" in check.missing
    assert any(i.key in ("reconcile", "equity", "position", "cash") and not i.ok
               for i in check.items)


def test_preconditions_pass_when_every_signal_is_healthy(system) -> None:
    _rm, _lc, gate = system
    check = assess_preconditions(gate, system[0])
    assert check.ok is True
    assert check.missing == []


def test_missing_gate_is_treated_as_untrusted() -> None:
    """读不到健康位时保守拒绝 —— 不猜「应该没事」。"""
    check = assess_preconditions(None, None)
    assert check.ok is False


def test_shutting_down_blocks_recovery(system) -> None:
    _rm, _lc, gate = system
    gate.shutting_down = True
    assert assess_preconditions(gate, system[0]).ok is False


# ---------------------------------------------------------------------------
# 自动路径: 前置不满足则原地不动
# ---------------------------------------------------------------------------


def test_automatic_recovery_refuses_when_preconditions_fail(system) -> None:
    _rm, lifecycle, gate = system
    _freeze(system)
    gate.reconciled = False

    result = perform_recovery(
        risk_manager=system[0], lifecycle=lifecycle, gate=gate, force=False
    )
    assert result["ok"] is False
    assert result["stage"] == "recovery_check"   # V15: 由「前置核对」升级为「恢复检查」
    # **状态一点没动** —— 这才是 fail-closed
    assert system[0].kill_switch.is_armed is True
    assert system[0].state_machine.state.value == "KILLED"
    assert lifecycle.current == "SAFE_MODE"


# ---------------------------------------------------------------------------
# 完整链路: 冻结 → 恢复 → 真的能交易
# ---------------------------------------------------------------------------


def test_recovery_unwinds_every_layer_of_a_drawdown_freeze(system) -> None:
    """核心回归: 回撤冻结之后, 恢复必须把三层**全部**解开。

    修 bug 之前, 只有 kill_switch 被解开, 风险态停在 KILLED、生命周期停在 SAFE_MODE
    —— 闸门因此仍然拒绝一切开仓, 用户会以为「恢复了但其实没有」。
    """
    rm, lifecycle, gate = system
    _to_trading(lifecycle)
    assert gate.can_open_position()[0] is True  # 冻结前可交易

    _freeze(system, origin=KILL_ORIGIN_AUTO_EQUITY)
    assert gate.can_open_position()[0] is False  # 冻结后不可交易

    result = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)

    assert result["ok"] is True
    assert rm.kill_switch.is_armed is False
    assert rm.state_machine.state.value == "NORMAL"
    assert lifecycle.current == "TRADING"
    # 最关键的一条: 恢复完之后闸门**真的**放行了
    ok, reason = gate.can_open_position()
    assert ok is True, f"恢复后仍不能开仓: {reason}"


def test_recovery_reports_each_step_for_the_operator() -> None:
    """页面要能逐步显示「✓ 已解除急停 / ✓ 风险态恢复 …」, 所以步骤必须回报。"""
    rm = RiskManager()
    lifecycle = SystemLifecycle()
    gate = TradingGate(rm, lifecycle)
    lifecycle.enter_safe_mode("x")
    rm.state_machine.kill("x")
    rm.kill_switch.arm("x", origin=KILL_ORIGIN_MANUAL)

    result = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)
    steps = " ".join(result["steps"])
    assert "急停" in steps
    assert "恢复核验" in steps
    assert "安全模式" in steps


def test_recovery_is_idempotent(system) -> None:
    """重复点「恢复」不该报错或把状态弄乱。"""
    rm, lifecycle, gate = system
    _freeze(system)
    perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)
    first = (rm.state_machine.state.value, lifecycle.current)

    second = perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)
    assert second["ok"] is True
    assert (rm.state_machine.state.value, lifecycle.current) == first


def test_recovery_from_risk_paused_returns_to_normal() -> None:
    rm = RiskManager()
    lifecycle = SystemLifecycle()
    gate = TradingGate(rm, lifecycle)
    _to_trading(lifecycle)
    rm.state_machine.pause("异常保护")

    perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)
    assert rm.state_machine.state.value == "NORMAL"


def test_recovery_never_touches_trading_permission_directly() -> None:
    """解冻**不等于**允许下单 —— 恢复后仍由闸门逐笔判定, 本流程不写 can_buy。

    构造一个「前置全过、但风险态被人为按住」的场景: 恢复应回到 NORMAL,
    随后任何阻断(如行情不健康)仍由闸门自行拒绝。
    """
    rm = RiskManager()
    lifecycle = SystemLifecycle()
    gate = TradingGate(rm, lifecycle)
    _to_trading(lifecycle)
    _freeze(system=(rm, lifecycle, gate))

    perform_recovery(risk_manager=rm, lifecycle=lifecycle, gate=gate, force=True)
    assert rm.state_machine.state.value == "NORMAL"

    # 恢复之后, 行情一旦不健康, 闸门照旧拒绝 —— 恢复流程没有绕过闸门
    gate.market_data_healthy = False
    ok, reason = gate.can_open_position()
    assert ok is False
    assert "行情" in reason


# ---------------------------------------------------------------------------
# 端点不再只做一半
# ---------------------------------------------------------------------------


def test_endpoint_uses_the_shared_flow() -> None:
    """端点必须走共享流程, 否则人工/自动两条路会各修一半。"""
    import inspect

    from at90_web import web_api_routes

    src = inspect.getsource(web_api_routes.emergency_recover)
    assert "perform_recovery" in src
    # 不允许退回「只 disarm」的旧写法。断言**实际调用**而不是文本出现 ——
    # 上面那段 docstring 正解释着这个旧写法, 按文本找会把自己人也拦下。
    code_lines = [
        ln.strip() for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert "rm.kill_switch.disarm()" not in code_lines


def test_kill_origin_is_reported_so_the_page_can_explain_itself() -> None:
    gate = TradingGate(RiskManager(), SystemLifecycle())
    ks = KillSwitch()
    ks.arm("x", origin=KILL_ORIGIN_AUTO_EQUITY)
    rm = RiskManager()
    rm.kill_switch = ks
    details = assess_preconditions(gate, rm).details
    assert details["kill_origin"] == KILL_ORIGIN_AUTO_EQUITY


def test_lifecycle_states_used_by_recovery_exist() -> None:
    """防止枚举改名后本模块静默失效(字符串比较不会有类型错误)。"""
    assert LifecycleState.SAFE_MODE.value == "SAFE_MODE"
    assert LifecycleState.READY.value == "READY"
    assert LifecycleState.TRADING.value == "TRADING"
