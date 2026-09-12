"""统一交易闸门(V11.2 P0-1 / P0-2)

单一 authoritative trading gate —— 所有新交易 / 减仓 / 撤单必须经过这里, 消除
「ExecutionEngine 自己判断一套 / RiskManager 判断一套 / run.py 再判断一套」的分歧。

六个维度合成最终许可:

    SystemLifecycle.can_trade   (生命周期态 READY/TRADING)
    AND RiskState.can_trade     (风险态 NORMAL)
    AND MarketDataHealthy       (行情有价、非静默)
    AND ExchangeHealthy         (交易所对账无 api_error / truth 完整)
    AND ReconciliationHealthy   (最近对账无 actionable 差异)
    AND CircuitBreaker.can_trade(资金熔断非 REDUCE_ONLY/PAUSE/KILL)

三个明确接口:

- `can_open_position()`  开新仓(BUY)
- `can_reduce_position()` 减仓(SELL / REDUCE)
- `can_cancel_order()`   撤单(风险收敛动作, 除 STOPPED 外放行)

方向语义(与生命周期/风险态对齐):

- 正常(READY/TRADING + NORMAL): BUY / SELL 皆可。
- DEGRADED / RECOVERY: 禁 BUY, 允许安全 REDUCE。
- SAFE_MODE: 禁 BUY, 数据可信时允许安全 REDUCE。
- KILLED: 禁 BUY, 禁减仓(急停冻结), 禁自动恢复。

健康信号(connection_ok / market_data_healthy / exchange_healthy / reconciled)由 run.py
各循环更新; 闸门本身只读不写(纯判定), 便于独立测试与审计。
"""

from typing import Any

from at01_common.logger import LoggerMixin
from at50_risk.fund_circuit_breaker import BreakerAction
from at50_risk.system_lifecycle import LifecycleState

# 开新仓被资金熔断阻断的动作档位
_BLOCK_OPEN = frozenset({BreakerAction.REDUCE_ONLY, BreakerAction.PAUSE, BreakerAction.KILL})


class TradingGate(LoggerMixin):
    """单一权威交易闸门(组合六维, 三接口)。"""

    def __init__(self, risk_manager, lifecycle, fund_breaker=None):
        self.risk_manager = risk_manager
        self.lifecycle = lifecycle
        self.fund_breaker = fund_breaker

        # 健康信号(由 run.py 各循环更新; 默认健康, 未就绪的维度由各自循环置 False)
        self.connection_ok = True
        self.market_data_healthy = True
        self.exchange_healthy = True
        self.reconciled = True
        self.last_breaker_action: BreakerAction = BreakerAction.NONE
        # V11.6 P0-2: BUY 安全契约补齐两维(此前散落在 run.py 调用方, 现收口到闸门)
        self.shutting_down = False          # 停机窗口: run.py stop() 置 True, 禁一切开仓/减仓
        self.critical_tasks_healthy = True  # 关键后台任务健康: critical 任务崩溃置 False, 禁开仓

    # ------------------------------------------------------------------ 三接口

    def can_open_position(self) -> tuple[bool, str]:
        """开新仓(BUY)。任一必要条件未满足即拒绝(V11.6 起含停机 + 关键任务两维)。"""
        # 0) 停机 / 关键后台任务(BUY 安全契约两维, 最基础, 先判)
        if self.shutting_down:
            return False, "系统停机中"
        if not self.critical_tasks_healthy:
            return False, "关键后台任务未运行"
        # 1) 风险层: 急停 / 熔断 / 风险态(NORMAL 才可买)
        if not self.risk_manager.can_buy():
            return False, self.risk_manager.block_reason or "风险禁止开仓"
        # 2) 生命周期层: READY/TRADING + 连接 + 对账
        ok, reason = self.lifecycle.can_trade(
            self.risk_manager.state_machine.state, self.connection_ok, self.reconciled
        )
        if not ok:
            return False, reason
        # 3) 数据 / 交易所健康
        if not self.market_data_healthy:
            return False, "行情数据不健康"
        if not self.exchange_healthy:
            return False, "交易所不健康"
        # 4) 资金熔断
        if self.last_breaker_action in _BLOCK_OPEN:
            return False, f"资金熔断: {self.last_breaker_action.value}"
        return True, ""

    def can_reduce_position(self) -> tuple[bool, str]:
        """减仓(SELL / REDUCE)。降级/恢复期允许安全离场; SAFE_MODE 数据可信时允许。"""
        # 0) 停机窗口: 停机即禁一切(与 _on_signal 早退语义一致, 防在途信号偷卖)
        if self.shutting_down:
            return False, "系统停机中"
        # 1) 风险层: can_sell(NORMAL 或 REDUCE_ONLY)
        if not self.risk_manager.can_sell():
            return False, self.risk_manager.block_reason or "风险禁止减仓"
        # 2) SAFE_MODE: 仅数据可信时允许减仓
        if self.lifecycle.state is LifecycleState.SAFE_MODE:
            if self._data_trusted():
                return True, "SAFE_MODE 安全减仓"
            return False, "SAFE_MODE 数据不可信, 禁减仓"
        # 3) 生命周期层: READY/TRADING/DEGRADED/RECOVERY + 连接
        ok, reason = self.lifecycle.can_reduce(
            self.risk_manager.state_machine.state, self.connection_ok
        )
        if not ok:
            return False, reason
        # 4) 数据 / 交易所健康(减仓也需数据可信以正确计价)
        if not self.market_data_healthy:
            return False, "行情数据不健康"
        if not self.exchange_healthy:
            return False, "交易所不健康"
        return True, ""

    def can_cancel_order(self) -> tuple[bool, str]:
        """撤单(风险收敛动作): 除 STOPPED 外放行(急停撤单也依赖此)。"""
        if self.lifecycle.is_stopped:
            return False, "系统已停止"
        return True, ""

    # ------------------------------------------------------------------ 辅助

    def _data_trusted(self) -> bool:
        """数据可信 = 行情健康 + 交易所健康 + 连接正常。"""
        return self.connection_ok and self.market_data_healthy and self.exchange_healthy

    def snapshot(self) -> dict[str, Any]:
        """闸门状态快照(供审计 / 面板 / 可观测性)。"""
        open_ok, open_reason = self.can_open_position()
        reduce_ok, reduce_reason = self.can_reduce_position()
        return {
            "lifecycle": self.lifecycle.current,
            "risk_state": self.risk_manager.state_machine.state.value,
            "connection_ok": self.connection_ok,
            "market_data_healthy": self.market_data_healthy,
            "exchange_healthy": self.exchange_healthy,
            "reconciled": self.reconciled,
            "breaker_action": self.last_breaker_action.value,
            "shutting_down": self.shutting_down,
            "critical_tasks_healthy": self.critical_tasks_healthy,
            "can_open_position": open_ok,
            "open_reason": open_reason,
            "can_reduce_position": reduce_ok,
            "reduce_reason": reduce_reason,
            "can_cancel_order": self.can_cancel_order()[0],
        }
