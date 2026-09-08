"""V11.4 P0-3 证明: 任何异常态下绝不继续 BUY(真实风控 + 统一闸门 + 静态防绕过)。

核心不变量: 系统进入任何异常态(急停/熔断/暂停/仅减仓/急停态/恢复核验/价格尖刺/
快速暴跌/行情静默/连续执行失败), 都必须且实际做到「不再下达任何 BUY 新仓」。

三层证明(用真实 RiskManager, 不用镜像 Fake):
1. 风险层: 10 类异常触发各自令 `RiskManager.can_buy() == False` 且
   `check(BUY)` 审批拒绝(approved=False), 即风险审批链(第二道闸)本身闭环;
2. 闸门层: 真实 `TradingGate`(真实 RiskManager + SystemLifecycle + FundCircuitBreaker)
   六维(风险/生命周期/连接/行情/交易所/对账/资金熔断)任一异常都令
   `can_open_position() == False`(开新仓唯一权威入口);
3. 静态层: run.py 两个 BUY 入口先过闸门才触达 execute; 执行层 execution_executor
   无风险许可权威(不得绕过闸门放行 BUY)。

配套: 与 test_v121(闸门组合)、test_v134(入口路由审计)互补, 本片聚焦「真实组件 +
异常触发的穷举 + 端到端审批拒绝」。
"""

import time
from collections import deque
from pathlib import Path

import pytest

from at50_strategy.strategy_base import Signal, SignalSide
from at60_risk.fund_circuit_breaker import BreakerAction, FundCircuitBreaker
from at60_risk.risk_manager import RiskManager
from at60_risk.system_lifecycle import SystemLifecycle
from at60_risk.trading_gate import TradingGate

SYMBOL = "SOLUSDT"


def _buy(price: float = 100.0, qty: float = 1.0) -> Signal:
    return Signal(symbol=SYMBOL, strategy="trend", side=SignalSide.BUY, price=price, quantity=qty)


# ---------------------------------------------------------------------------
# 异常工厂: 每个返回一个已处于「异常态」的真实 RiskManager(用真实触发方法, 非手改状态)
# ---------------------------------------------------------------------------

def _kill_switch() -> RiskManager:
    rm = RiskManager()
    rm.kill_switch.arm("异常-急停")
    return rm


def _breaker_open() -> RiskManager:
    rm = RiskManager()
    rm.breaker.manual_trip("异常-熔断")
    return rm


def _paused() -> RiskManager:
    rm = RiskManager()
    rm.pause("异常-暂停")
    return rm


def _reduce_only() -> RiskManager:
    rm = RiskManager()
    rm.reduce_only("异常-仅减仓")
    return rm


def _killed() -> RiskManager:
    rm = RiskManager()
    rm.kill_switch.arm("异常-急停")
    rm.state_machine.kill("异常-急停")
    return rm


def _recovery_check() -> RiskManager:
    rm = RiskManager()
    rm.kill_switch.arm("异常-急停")
    rm.state_machine.kill("异常-急停")
    rm.state_machine.reset()
    return rm


def _tick_spike() -> RiskManager:
    rm = RiskManager()
    rm.check_tick_anomaly(SYMBOL, 100.0)
    rm.check_tick_anomaly(SYMBOL, 104.0)  # +4% > 3% 阈值
    return rm


def _fast_crash() -> RiskManager:
    rm = RiskManager()
    rm._price_history[SYMBOL] = deque([(time.time() - 10.0, 100.0)])
    rm.check_fast_crash(SYMBOL, 85.0)  # -15% > 10% 阈值
    return rm


def _market_silence() -> RiskManager:
    rm = RiskManager()
    rm._last_tick_time = time.time() - 31.0  # 超过 30s 静默阈值
    rm.check_market_silence()
    return rm


def _exec_errors() -> RiskManager:
    rm = RiskManager()
    for _ in range(3):
        rm.record_execution_error()
    return rm


ANOMALIES = [
    ("急停开关", _kill_switch),
    ("熔断器", _breaker_open),
    ("异常暂停", _paused),
    ("仅减仓", _reduce_only),
    ("急停态", _killed),
    ("恢复核验", _recovery_check),
    ("价格尖刺", _tick_spike),
    ("快速暴跌", _fast_crash),
    ("行情静默", _market_silence),
    ("连续执行失败", _exec_errors),
]
_IDS = [n for n, _ in ANOMALIES]


# ---------------------------------------------------------------------------
# 层 1: 风险层 —— 每一异常都令 can_buy()==False 且 check(BUY) 拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,factory", ANOMALIES, ids=_IDS)
def test_anomaly_blocks_buy(name: str, factory):
    """任一异常后, 风险层拒绝开新仓(can_buy / can_trade 双 False)。"""
    rm = factory()
    assert not rm.can_buy(), f"[{name}] 异常后仍可 BUY"
    assert not rm.can_trade(), f"[{name}] 异常后仍可交易"


@pytest.mark.parametrize("name,factory", ANOMALIES, ids=_IDS)
async def test_anomaly_rejects_buy_signal_end_to_end(name: str, factory, db_tables):
    """端到端: 异常态下 BUY 信号经真实审批链 `check()` 被拒绝(approved=False)。"""
    rm = factory()
    decision = await rm.check(_buy(), {SYMBOL: 100.0})
    assert decision.approved is False, f"[{name}] 异常后 BUY 信号被批准"
    assert decision.quantity == 0.0
    await rm.flush_events()


def test_reduce_only_allows_sell_but_not_buy():
    """方向语义: 仅减仓态仍可离场(SELL), 但绝不 BUY。"""
    rm = _reduce_only()
    assert not rm.can_buy()
    assert rm.can_sell()


def test_killed_blocks_buy_and_sell():
    """急停态: 禁开禁减(全冻结)。"""
    rm = _killed()
    assert not rm.can_buy()
    assert not rm.can_sell()


# ---------------------------------------------------------------------------
# 层 2: 闸门层 —— 真实 TradingGate 六维任一异常都令 can_open_position()==False
# ---------------------------------------------------------------------------

def _ready_gate() -> tuple[TradingGate, RiskManager, SystemLifecycle, FundCircuitBreaker]:
    rm = RiskManager()
    lc = SystemLifecycle()
    assert lc.warm_up() and lc.sync() and lc.self_check() and lc.ready() and lc.start_trading()
    breaker = FundCircuitBreaker()
    return TradingGate(rm, lc, breaker), rm, lc, breaker


def _gate_lifecycle(state: str) -> TradingGate:
    """构造处于指定生命周期态的闸门(真实迁移序列, 非手改)。"""
    rm = RiskManager()
    lc = SystemLifecycle()
    seq = {
        "INIT": [],
        "WARMING_UP": ["warm_up"],
        "SYNCING": ["warm_up", "sync"],
        "SELF_CHECK": ["warm_up", "sync", "self_check"],
        "READY": ["warm_up", "sync", "self_check", "ready"],
        "TRADING": ["warm_up", "sync", "self_check", "ready", "start_trading"],
        "DEGRADED": ["warm_up", "sync", "self_check", "ready", "start_trading"],
        "RECOVERY": ["warm_up", "sync", "self_check", "ready", "start_trading"],
        "SAFE_MODE": ["warm_up", "sync", "self_check", "ready"],
        "STOPPED": ["warm_up", "sync", "self_check", "ready"],
    }[state]
    for m in seq:
        getattr(lc, m)()
    if state == "DEGRADED":
        lc.degrade("降级")
    elif state == "RECOVERY":
        lc.degrade("降级")
        lc.recover()
    elif state == "SAFE_MODE":
        lc.enter_safe_mode("安全模式")
    elif state == "STOPPED":
        lc.stop()
    return TradingGate(rm, lc, FundCircuitBreaker())


def _gate(fn):
    """把「变异某维度」的闭包包装为返回闸门的工厂。"""
    def make():
        gate, rm, lc, br = _ready_gate()
        fn(gate, rm, lc, br)
        return gate
    return make


_GATE_ANOMALIES = [
    ("急停开关", _gate(lambda g, rm, *_a: rm.kill_switch.arm("x"))),
    ("熔断器", _gate(lambda g, rm, *_a: rm.breaker.manual_trip("x"))),
    ("异常暂停", _gate(lambda g, rm, *_a: rm.pause("x"))),
    ("仅减仓", _gate(lambda g, rm, *_a: rm.reduce_only("x"))),
    ("急停态", _gate(lambda g, rm, *_a: (rm.kill_switch.arm("x"), rm.state_machine.kill("x")))),
    ("恢复核验", _gate(lambda g, rm, *_a: (rm.kill_switch.arm("x"), rm.state_machine.kill("x"), rm.state_machine.reset()))),
    ("行情连接断开", _gate(lambda g, *_a: setattr(g, "connection_ok", False))),
    ("行情不健康", _gate(lambda g, *_a: setattr(g, "market_data_healthy", False))),
    ("交易所不健康", _gate(lambda g, *_a: setattr(g, "exchange_healthy", False))),
    ("对账未通过", _gate(lambda g, *_a: setattr(g, "reconciled", False))),
    ("资金熔断-仅减仓", _gate(lambda g, *_a: setattr(g, "last_breaker_action", BreakerAction.REDUCE_ONLY))),
    ("资金熔断-暂停", _gate(lambda g, *_a: setattr(g, "last_breaker_action", BreakerAction.PAUSE))),
    ("资金熔断-急停", _gate(lambda g, *_a: setattr(g, "last_breaker_action", BreakerAction.KILL))),
]
_GATE_IDS = [n for n, _ in _GATE_ANOMALIES]


def test_gate_normal_allows_open():
    """基准: 无异常时闸门放行开新仓。"""
    gate, *_ = _ready_gate()
    assert gate.can_open_position()[0]


@pytest.mark.parametrize("name,factory", _GATE_ANOMALIES, ids=_GATE_IDS)
def test_gate_anomaly_blocks_open(name: str, factory):
    """统一闸门: 任一维异常都禁开新仓(BUY 唯一权威入口)。"""
    ok, reason = factory().can_open_position()
    assert not ok, f"[{name}] 异常后闸门仍放行开新仓"
    assert reason, f"[{name}] 拒绝但无原因(不利于审计)"


@pytest.mark.parametrize(
    "state",
    ["INIT", "WARMING_UP", "SYNCING", "SELF_CHECK", "DEGRADED", "RECOVERY", "SAFE_MODE", "STOPPED"],
)
def test_gate_lifecycle_non_trading_blocks_open(state: str):
    """生命周期: 仅 READY/TRADING 可开新仓, 其余全部拒绝。"""
    gate = _gate_lifecycle(state)
    assert not gate.can_open_position()[0], f"[{state}] 仍可开新仓"


# ---------------------------------------------------------------------------
# 层 3: 静态审计 —— BUY 只经闸门放行, 执行层无风险许可权威
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_run_buy_paths_double_gated_before_execute():
    """run.py BUY 入口先过统一闸门 + 风险审批双闸, 任一拒绝即不触达 execute。"""
    run = _read("run.py")
    assert "self.trading_gate.can_open_position()" in run
    assert "self.trading_gate.can_reduce_position()" in run
    # _on_signal 第二道闸: 风控审批链 check(BUY 走 can_buy)
    assert "risk_manager.check(sig, last_prices)" in run


def test_executor_has_no_risk_permission_authority():
    """执行层是「终端提交方」: 无 can_open_position, 也不得直调 risk_manager.can_buy/sell 放行。"""
    exe = _read("at50_execution/execution_executor.py")
    assert "can_open_position" not in exe
    assert "risk_manager.can_buy" not in exe
    assert "risk_manager.can_sell" not in exe
