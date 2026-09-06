"""
运行时状态容器

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
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """系统总览"""
        from common.config.settings import get_settings

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
        return out


# 全局单例
system_state = SystemState()
