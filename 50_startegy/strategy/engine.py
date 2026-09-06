"""
策略引擎

管理所有策略实例,接收分析快照,聚合各策略信号并回调(风控引擎)。
"""

from typing import Any, Awaitable, Callable, Optional

from analytics.engine import AnalyticsEngine, MarketAnalytics
from common.config.settings import get_settings
from common.utils.logger import LoggerMixin
from strategy.ai_advisor import AIAdvisor
from strategy.base import BaseStrategy, Signal
from strategy.buy import BuyStrategy
from strategy.grid import GridStrategy
from strategy.sell import SellStrategy
from strategy.trend import TrendStrategy

SignalCallback = Callable[[Signal], Awaitable[None]]

# 持仓查询: symbol -> (quantity, avg_price, peak_price)
PositionProvider = Callable[[str], Optional[tuple[float, float, float]]]


class StrategyEngine(LoggerMixin):
    """策略引擎"""

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        on_signal: Optional[SignalCallback] = None,
    ):
        self.settings = get_settings()
        self.symbols = symbols or self.settings.symbol_list
        self.on_signal = on_signal

        self.strategies: dict[str, BaseStrategy] = {}
        self.ai_advisor = AIAdvisor()
        self.position_provider: Optional[PositionProvider] = None
        self.signal_count = 0
        self._persist_signal: Optional[Callable[[Signal, str], Awaitable[None]]] = None

    def setup(self) -> None:
        """按配置装配策略"""
        enabled = self.settings.enabled_strategies

        if "buy" in enabled:
            self.strategies["buy"] = BuyStrategy(self.symbols)
        if "sell" in enabled:
            sell = SellStrategy(self.symbols)
            sell.position_provider = self.position_provider
            self.strategies["sell"] = sell
        if "grid" in enabled:
            self.strategies["grid"] = GridStrategy(self.symbols)
        if "trend" in enabled:
            self.strategies["trend"] = TrendStrategy(self.symbols)

        self.logger.info("策略装配完成", strategies=list(self.strategies))

    # ---------- 主入口 ----------

    async def on_analytics(self, symbol: str, analytics: MarketAnalytics) -> None:
        """分析引擎回调:运行所有策略生成信号"""
        signals: list[Signal] = []
        for strategy in self.strategies.values():
            if not strategy.enabled:
                continue
            try:
                result = strategy.on_market(analytics)
                if result:
                    signals.extend(result)
            except Exception:
                self.logger.exception("策略执行异常", strategy=strategy.name)

        for signal in signals:
            self.signal_count += 1
            self.logger.info(
                "策略信号",
                strategy=signal.strategy,
                symbol=signal.symbol,
                side=signal.side.value,
                price=signal.price,
                reason=signal.reason,
            )
            if self._persist_signal:
                await self._persist_signal(signal, "pending")
            if self.on_signal:
                await self.on_signal(signal)

    # ---------- 成交回调 ----------

    async def on_fill(self, signal: Signal, fill_price: float, fill_qty: float) -> None:
        """通知各策略成交"""
        strategy = self.strategies.get(signal.strategy)
        if strategy:
            strategy.on_fill(signal, fill_price, fill_qty)

    # ---------- AI 顾问 ----------

    async def run_ai_advisor(self, analytics_snapshot: dict[str, Any]) -> dict[str, Any]:
        """运行 AI 顾问分析,返回 {symbol: advice}"""
        if not self.ai_advisor.enabled:
            return {}

        results: dict[str, Any] = {}
        for symbol in self.symbols:
            a = analytics_snapshot.get("symbols", {}).get(symbol)
            if not a:
                continue

            pos = None
            if self.position_provider:
                p = self.position_provider(symbol)
                if p and p[0] > 0:
                    pos = {"quantity": p[0], "avg_price": p[1], "peak_price": p[2]}

            advice = await self.ai_advisor.analyze(symbol, a, pos)
            if advice:
                results[symbol] = advice
                self.logger.info("AI建议", symbol=symbol, **{k: v for k, v in advice.items() if k != "raw"})
                await self._persist_ai_advice(symbol, advice)
        return results

    async def _persist_ai_advice(self, symbol: str, advice: dict[str, Any]) -> None:
        """持久化 AI 建议"""
        try:
            from common.config.database import AsyncSessionLocal
            from common.models import AIAdvice

            async with AsyncSessionLocal() as session:
                session.add(
                    AIAdvice(
                        symbol=symbol,
                        advice=advice["advice"],
                        confidence=advice.get("confidence", 0.0),
                        summary=advice.get("summary", ""),
                        raw_response=advice.get("raw"),
                    )
                )
                await session.commit()
        except Exception:
            self.logger.exception("AI建议持久化失败")

    # ---------- 状态 ----------

    def status(self) -> dict[str, Any]:
        return {
            "strategies": {name: s.status() for name, s in self.strategies.items()},
            "signal_count": self.signal_count,
            "ai_enabled": self.ai_advisor.enabled,
        }

    def get_grid_strategy(self) -> Optional[GridStrategy]:
        """获取网格策略实例(供持仓对账)"""
        return self.strategies.get("grid")  # type: ignore[return-value]

    async def close(self) -> None:
        await self.ai_advisor.close()
