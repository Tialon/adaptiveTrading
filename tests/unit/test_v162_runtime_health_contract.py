"""V11.6 P1-2 证明: 运行时健康契约 —— health.can_buy/can_sell 直接取自统一闸门(单一权威)。

契约:
- `build_runtime_health` 输出 `state`(状态名, 与 `status` 同值)/ `can_buy` / `can_sell` /
  `buy_block_reason` / `sell_block_reason` 五字段;
- `health.can_buy == TradingGate.can_open_position()[0]`(而非另起一套判定), 同理
  `health.can_sell == can_reduce_position()[0]`, 拒绝原因逐字对齐;
- 闸门未就绪(未注入)时保守拒绝(can_buy=False), 不虚报「可买」;
- 与 P0-2 联动: 停机(shutting_down)/ 关键任务崩溃(critical_tasks_healthy=False)两维
  经闸门 → 正确反映到 health.can_buy=False。

本片与 test_v154(快照聚合 + 七态分类器)互补: v154 测「聚合是否正确」, 本片测
「交易许可是否与统一闸门逐字一致」。
"""

import time

from at01_common.runtime_health import STATUS_TRADING, build_runtime_health
from at10_web.web_state import SystemState
from at60_risk.fund_circuit_breaker import FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate


def _healthy_state() -> tuple[SystemState, TradingGate]:
    """构造一个「理应可交易」的 SystemState + 真实 TradingGate(真实 RiskManager/生命周期)。"""
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


def test_health_can_buy_equals_gate_when_healthy():
    """健康态: health.can_buy/can_sell 与闸门逐字一致(单一权威)。"""
    st, gate = _healthy_state()
    h = build_runtime_health(st)

    assert h["can_buy"] is True
    assert h["can_sell"] is True
    assert h["can_buy"] == gate.can_open_position()[0]
    assert h["can_sell"] == gate.can_reduce_position()[0]
    assert h["buy_block_reason"] == gate.can_open_position()[1] == ""
    assert h["sell_block_reason"] == gate.can_reduce_position()[1] == ""


def test_health_reflects_gate_buy_block_reason():
    """急停后: health.can_buy=False 且 buy_block_reason 与闸门逐字一致。"""
    st, gate = _healthy_state()
    gate.risk_manager.kill_switch.arm("测试急停")

    h = build_runtime_health(st)
    ok, reason = gate.can_open_position()

    assert h["can_buy"] is ok is False
    assert h["buy_block_reason"] == reason
    assert reason  # 非空, 可审计


def test_health_state_equals_status():
    """state 为规范化状态名, 与 status 同值(兼容旧面板)。"""
    st, _ = _healthy_state()
    h = build_runtime_health(st)
    assert h["state"] == h["status"] == STATUS_TRADING


def test_health_gate_unavailable_conservative():
    """闸门未注入: 保守拒绝, 不虚报「可买」。"""
    h = build_runtime_health(SystemState())
    assert h["can_buy"] is False
    assert h["can_sell"] is False
    assert h["buy_block_reason"] == "交易闸门未就绪"
    assert h["sell_block_reason"] == "交易闸门未就绪"


def test_health_reflects_shutdown_and_critical_tasks():
    """P0-2 两维经闸门 → health 正确反映(停机 / 关键任务未运行均禁 BUY)。"""
    st, gate = _healthy_state()

    gate.shutting_down = True
    h = build_runtime_health(st)
    assert h["can_buy"] is False
    assert "停机" in h["buy_block_reason"]

    gate.shutting_down = False
    gate.critical_tasks_healthy = False
    h = build_runtime_health(st)
    assert h["can_buy"] is False
    assert "关键后台任务" in h["buy_block_reason"]
