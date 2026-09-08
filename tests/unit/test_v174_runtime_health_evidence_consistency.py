"""V11.7 P1-6 证明: Runtime health 与 evidence 一致性 —— health.can_buy/can_sell
必须逐字等于统一闸门 TradingGate 的判定(单一权威), 绝不虚报「可买」。

契约(见 V11.7 spec §十二):
- `health.can_buy == TradingGate.can_open_position()[0]`;
- `health.can_sell == TradingGate.can_reduce_position()[0]`;
- 若 health 显示 can_buy=true 而闸门实际禁止 → 严重缺陷(本片用逐维阻断参数化测试锁定
  反向不变式: 每一维被阻断时, health 与闸门仍逐字一致, 不会出现「health 可买但闸门禁买」)。

与 test_v162(V11.6 P1-2 契约)互补: v162 测「交易许可取自统一闸门」的基本契约(健康态 /
急停 / 停机 / 关键任务), 本片把该契约扩展到闸门全部六个维度 + 资金熔断, 逐维参数化,
确保「任何单一维度阻断都不会造成 health 与闸门分歧」。

阻断维度覆盖:
- 停机(shutting_down)、关键任务(critical_tasks_healthy) —— V11.6 BUY 安全契约两维;
- 急停(kill_switch)、生命周期降级(DEGRADED);
- 行情数据不健康(market_data_healthy)、交易所不健康(exchange_healthy)、对账未通过(reconciled);
- 资金熔断(REDUCE_ONLY / PAUSE)。
"""

import time

import pytest

from at01_common.runtime_health import build_runtime_health
from at10_web.web_state import SystemState
from at60_risk.fund_circuit_breaker import BreakerAction, FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate


def _healthy_state() -> tuple[SystemState, TradingGate]:
    """构造「理应可交易」的 SystemState + 真实 TradingGate(真实 RiskManager/生命周期)。"""
    rm = RiskManager()
    lc = SystemLifecycle()
    assert lc.warm_up() and lc.sync() and lc.self_check() and lc.ready() and lc.start_trading()
    gate = TradingGate(rm, lc, FundCircuitBreaker())

    st = SystemState()
    st.running = True
    st.started_at = time.time() - 10.0
    st.risk_manager = rm
    st.lifecycle = lc
    st.trading_gate = gate
    return st, gate


# 逐维阻断器: 每个只改一处闸门输入, 使「该维度单独阻断」时能校验 health 是否仍与闸门一致。
def _blk_shutdown(g: TradingGate) -> None:
    g.shutting_down = True


def _blk_critical_tasks(g: TradingGate) -> None:
    g.critical_tasks_healthy = False


def _blk_kill_switch(g: TradingGate) -> None:
    g.risk_manager.kill_switch.arm("测试急停")


def _blk_degraded(g: TradingGate) -> None:
    g.lifecycle.degrade("测试降级")


def _blk_market_unhealthy(g: TradingGate) -> None:
    g.market_data_healthy = False


def _blk_exchange_unhealthy(g: TradingGate) -> None:
    g.exchange_healthy = False


def _blk_not_reconciled(g: TradingGate) -> None:
    g.reconciled = False


def _blk_breaker_reduce_only(g: TradingGate) -> None:
    g.last_breaker_action = BreakerAction.REDUCE_ONLY


def _blk_breaker_pause(g: TradingGate) -> None:
    g.last_breaker_action = BreakerAction.PAUSE


_BLOCKERS = [
    ("shutting_down", _blk_shutdown),
    ("critical_tasks_healthy=False", _blk_critical_tasks),
    ("kill_switch armed", _blk_kill_switch),
    ("lifecycle DEGRADED", _blk_degraded),
    ("market_data_healthy=False", _blk_market_unhealthy),
    ("exchange_healthy=False", _blk_exchange_unhealthy),
    ("reconciled=False", _blk_not_reconciled),
    ("breaker REDUCE_ONLY", _blk_breaker_reduce_only),
    ("breaker PAUSE", _blk_breaker_pause),
]


def test_health_mirrors_gate_when_healthy():
    """基线(无阻断): health.can_buy/can_sell 与闸门逐字一致(单一权威)。"""
    st, gate = _healthy_state()
    h = build_runtime_health(st)

    assert h["can_buy"] is True
    assert h["can_sell"] is True
    assert h["can_buy"] == gate.can_open_position()[0]
    assert h["can_sell"] == gate.can_reduce_position()[0]
    assert h["buy_block_reason"] == gate.can_open_position()[1] == ""
    assert h["sell_block_reason"] == gate.can_reduce_position()[1] == ""


@pytest.mark.parametrize("label,mutate", _BLOCKERS)
def test_health_mirrors_gate_under_each_blocker(label, mutate):
    """逐维阻断: health 仍与闸门逐字一致, 且该维度确实阻断了至少一个方向(测试有效性)。"""
    st, gate = _healthy_state()
    mutate(gate)

    buy_ok, buy_reason = gate.can_open_position()
    sell_ok, sell_reason = gate.can_reduce_position()

    # 测试有效性: 阻断器必须真实阻断(否则无法证明「阻断时仍一致」)。
    assert not (buy_ok and sell_ok), f"{label}: 阻断器未阻断任何方向, 测试无效"

    h = build_runtime_health(st)

    # P1-6 核心契约: health 与闸门逐字一致(不虚报 can_buy / can_sell)。
    assert h["can_buy"] is buy_ok, f"{label}: can_buy 分歧 {h['can_buy']!r} != {buy_ok!r}"
    assert h["can_sell"] is sell_ok, f"{label}: can_sell 分歧 {h['can_sell']!r} != {sell_ok!r}"
    assert h["buy_block_reason"] == buy_reason, label
    assert h["sell_block_reason"] == sell_reason, label

    # 反向不变式(严重缺陷防线): health 绝不在闸门禁止时显示可买/可卖。
    if h["can_buy"]:
        assert buy_ok is True, f"{label}: health 虚报 can_buy=true"
    if h["can_sell"]:
        assert sell_ok is True, f"{label}: health 虚报 can_sell=true"


def test_health_never_false_positive_buy():
    """直接锁定反向不变式: 任一单维阻断都不会出现 health.can_buy=true 而闸门禁买。"""
    for label, mutate in _BLOCKERS:
        st, gate = _healthy_state()
        mutate(gate)
        h = build_runtime_health(st)
        gate_buy_ok, _ = gate.can_open_position()
        assert not (h["can_buy"] and not gate_buy_ok), (
            f"{label}: health.can_buy=true 但闸门禁止 → 严重缺陷"
        )


def test_health_gate_unavailable_conservative():
    """闸门未注入: 保守拒绝(can_buy=False), 不虚报「可买」(与 v162 一致, 此处补齐证据位)。"""
    h = build_runtime_health(SystemState())
    assert h["can_buy"] is False
    assert h["can_sell"] is False
    assert h["buy_block_reason"] == "交易闸门未就绪"
    assert h["sell_block_reason"] == "交易闸门未就绪"
