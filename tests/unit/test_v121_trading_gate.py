"""V11.2 P0-1/P0-2 统一交易闸门(TradingGate)单元测试。

覆盖: 六维组合(生命周期态 + 风险态 + 行情健康 + 交易所健康 + 对账 + 资金熔断)/
三接口(can_open_position / can_reduce_position / can_cancel_order)/
方向语义(降级恢复期禁买可减、SAFE_MODE 数据可信可减、KILLED 全禁)。
"""

import pytest

from at60_risk.fund_circuit_breaker import BreakerAction
from at60_risk.risk_state import RiskStateMachine
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate


class FakeRiskManager:
    """镜像 RiskManager 的闸门语义(急停/熔断/风险态), 免 DB。"""

    def __init__(self, state_machine: RiskStateMachine):
        self.state_machine = state_machine
        self.kill_switch_armed = False
        self.breaker_open = False

    def can_buy(self) -> bool:
        return not self.kill_switch_armed and not self.breaker_open and self.state_machine.can_buy()

    def can_sell(self) -> bool:
        return not self.kill_switch_armed and not self.breaker_open and self.state_machine.can_sell()

    @property
    def block_reason(self) -> str:
        if self.kill_switch_armed:
            return "急停中: test"
        if self.breaker_open:
            return "熔断中: test"
        return self.state_machine.block_reason()


def _go_trading(lc: SystemLifecycle) -> SystemLifecycle:
    lc.warm_up(); lc.sync(); lc.self_check(); lc.ready(); lc.start_trading()
    return lc


def _make_gate() -> tuple[TradingGate, FakeRiskManager, SystemLifecycle]:
    sm = RiskStateMachine()
    rm = FakeRiskManager(sm)
    lc = _go_trading(SystemLifecycle())
    gate = TradingGate(rm, lc)
    return gate, rm, lc


# ---------------------------------------------------------------------------
# 正常态
# ---------------------------------------------------------------------------

def test_normal_allows_open_and_reduce():
    gate, _, _ = _make_gate()
    assert gate.can_open_position()[0]
    assert gate.can_reduce_position()[0]
    assert gate.can_cancel_order()[0]


# ---------------------------------------------------------------------------
# 生命周期态
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["DEGRADED", "RECOVERY"])
def test_degraded_recovery_blocks_open_allows_reduce(state):
    gate, _, lc = _make_gate()
    if state == "DEGRADED":
        lc.degrade("x")
    else:
        lc.degrade("x"); lc.recover()
    assert not gate.can_open_position()[0]
    assert gate.can_reduce_position()[0]


def test_safe_mode_blocks_open_and_reduces_when_trusted():
    gate, _, lc = _make_gate()
    lc.enter_safe_mode("x")
    assert not gate.can_open_position()[0]
    assert gate.can_reduce_position()[0]  # 数据可信


def test_safe_mode_blocks_reduce_when_data_untrusted():
    gate, _, lc = _make_gate()
    lc.enter_safe_mode("x")
    gate.market_data_healthy = False
    assert not gate.can_reduce_position()[0]


def test_startup_blocks_open_and_reduce():
    sm = RiskStateMachine()
    rm = FakeRiskManager(sm)
    lc = SystemLifecycle()  # INIT
    gate = TradingGate(rm, lc)
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


# ---------------------------------------------------------------------------
# 风险态
# ---------------------------------------------------------------------------

def test_reduce_only_blocks_open_allows_reduce():
    gate, rm, _ = _make_gate()
    rm.state_machine.reduce_only("x")
    assert not gate.can_open_position()[0]
    assert gate.can_reduce_position()[0]


def test_paused_blocks_open_and_reduce():
    gate, rm, _ = _make_gate()
    rm.state_machine.pause("x")
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


def test_killed_blocks_open_and_reduce():
    gate, rm, _ = _make_gate()
    rm.state_machine.kill("x")
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


def test_kill_switch_blocks_open_and_reduce():
    gate, rm, _ = _make_gate()
    rm.kill_switch_armed = True
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


def test_breaker_open_blocks_open_and_reduce():
    gate, rm, _ = _make_gate()
    rm.breaker_open = True
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


# ---------------------------------------------------------------------------
# 数据 / 交易所 / 对账健康
# ---------------------------------------------------------------------------

def test_market_unhealthy_blocks_both():
    gate, _, _ = _make_gate()
    gate.market_data_healthy = False
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


def test_exchange_unhealthy_blocks_both():
    gate, _, _ = _make_gate()
    gate.exchange_healthy = False
    assert not gate.can_open_position()[0]
    assert not gate.can_reduce_position()[0]


def test_not_reconciled_blocks_open_allows_reduce():
    gate, _, _ = _make_gate()
    gate.reconciled = False
    assert not gate.can_open_position()[0]
    assert gate.can_reduce_position()[0]  # 减仓闸门不校验对账


# ---------------------------------------------------------------------------
# 资金熔断
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [BreakerAction.REDUCE_ONLY, BreakerAction.PAUSE, BreakerAction.KILL])
def test_breaker_action_blocks_open(action):
    gate, _, _ = _make_gate()
    gate.last_breaker_action = action
    assert not gate.can_open_position()[0]


def test_breaker_none_allows_open():
    gate, _, _ = _make_gate()
    gate.last_breaker_action = BreakerAction.NONE
    assert gate.can_open_position()[0]


# ---------------------------------------------------------------------------
# 撤单 / 快照
# ---------------------------------------------------------------------------

def test_cancel_allowed_unless_stopped():
    gate, _, lc = _make_gate()
    assert gate.can_cancel_order()[0]
    lc.stop()
    assert not gate.can_cancel_order()[0]


def test_snapshot_shape():
    gate, _, _ = _make_gate()
    snap = gate.snapshot()
    for key in (
        "lifecycle", "risk_state", "connection_ok", "market_data_healthy",
        "exchange_healthy", "reconciled", "breaker_action",
        "can_open_position", "can_reduce_position", "can_cancel_order",
    ):
        assert key in snap
