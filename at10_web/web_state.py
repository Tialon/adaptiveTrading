"""
运行时状态容器(状态层)

main.py 启动后将各引擎实例注册进来,Web 层通过它访问运行数据。
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SystemState:
    """系统运行时状态(各引擎句柄)"""

    running: bool = False
    started_at: Optional[float] = None
    market_engine: Any = None
    analytics_engine: Any = None
    strategy_engine: Any = None
    risk_manager: Any = None
    execution_engine: Any = None
    regime_engine: Any = None  # V2.0: 市场环境引擎
    trading_gate: Any = None  # V11.2 P0-2: 统一交易闸门
    metrics: Any = None  # V11.1 P1-4: 生产可观测性指标
    lifecycle: Any = None  # V11.2 P1-1: 顶层生命周期状态机
    last_alerts: list[Any] = field(default_factory=list)  # V11.2 P1-2: 最近一次指标告警
    supervisor: Any = None  # V11.5 P1-1: 后台任务监督器(active tasks / task failures)
    last_reconcile_at: Optional[float] = None  # V11.5 P1-1: 最近一次对账完成时间
    last_error: Optional[dict] = None  # V11.5 P1-1: 最近一次运行时错误 {ts, source, message}
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """系统总览"""
        from at01_common.settings import get_settings

        settings = get_settings()
        out: dict[str, Any] = {
            "app": settings.app_name,
            "version": settings.app_version,
            "running": self.running,
            "paper": settings.paper_trading,
            "symbols": settings.symbol_list,
        }
        if self.risk_manager:
            last_prices = {
                s: st.last_price for s, st in (self.market_engine.state.items() if self.market_engine else [])
            }
            risk_status = self.risk_manager.status()
            out["equity"] = round(self.risk_manager.equity(last_prices), 2)
            out["risk"] = risk_status
        if self.execution_engine:
            out["execution"] = self.execution_engine.status()
        if self.strategy_engine:
            out["strategy"] = self.strategy_engine.status()
        if self.regime_engine:
            out["regime"] = self.regime_engine.snapshot()
        if self.trading_gate:
            out["trading_gate"] = self.trading_gate.snapshot()
        if self.metrics:
            out["metrics"] = self.metrics.snapshot()
        if self.lifecycle:
            out["lifecycle"] = {
                "state": self.lifecycle.current,
                "reason": self.lifecycle.reason,
                "transitions": len(self.lifecycle.history),
                "last_transition_at": self.lifecycle.last_transition_at,
            }
        if self.last_alerts:
            out["alerts"] = [a.to_dict() for a in self.last_alerts]
        return out


# 全局单例
system_state = SystemState()
