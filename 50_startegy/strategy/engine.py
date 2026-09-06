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

    def setup(self) -> None:
        """按配置装配策略(V2.0: entry/exit 命名,兼容旧 buy/sell 配置)"""
        enabled = self.settings.enabled_strategies

        if "buy" in enabled or "entry" in enabled:
            self.strategies["buy"] = BuyStrategy(self.symbols)
        if "sell" in enabled or "exit" in enabled:
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
        """分析引擎回调:运行所有策略生成信号(V2.0: regime 调整 + 标准信号落库)"""
        signals: list[Signal] = []

        # Market Regime 调整(V2.0)
        regime = analytics.regime
        adjustment = None
        if regime:
            from analytics.regime import MarketRegimeEngine

            adjustment = MarketRegimeEngine.strategy_adjustment(regime)

        for strategy in self.strategies.values():
            if not strategy.enabled:
                continue
            # BEAR/PANIC 下禁用网格
            if adjustment and strategy.name == "grid" and not adjustment.get("grid_enabled", True):
                continue
            # PANIC 下禁用所有买入策略
            if (
                adjustment
                and regime == "PANIC"
                and strategy.name in ("entry", "trend")
                and strategy.__class__.__name__ != "TrendStrategy"
            ):
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
                score=signal.score,
                reason=signal.reason_str[:120],
            )
            await self._persist_signal(signal)
            if self.on_signal:
                await self.on_signal(signal)

    async def _persist_signal(self, signal: Signal) -> None:
        """标准信号落库(strategy_signal: score/reason/indicators)"""
        import json as _json

        from common.config.database import AsyncSessionLocal
        from common.models import Signal as SignalModel

        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    SignalModel(
                        symbol=signal.symbol,
                        strategy=signal.strategy,
                        side=signal.side.value,
                        price=signal.price,
                        quantity=signal.quantity,
                        quote_amount=signal.quote_amount,
                        reason=signal.reason_str[:500],
                        score=signal.score,
                        indicators=_json.dumps(signal.indicators, ensure_ascii=False)[:2000],
                        status="pending",
                    )
                )
                await session.commit()
        except Exception:
            self.logger.exception("信号落库失败")

    # ---------- 成交回调 ----------

    async def on_fill(self, signal: Signal, fill_price: float, fill_qty: float) -> None:
        """通知各策略成交"""
        strategy = self.strategies.get(signal.strategy)
        if strategy:
            strategy.on_fill(signal, fill_price, fill_qty)

    # ---------- AI 顾问(V2.0: 只出参数建议,不交易) ----------

    async def run_ai_advisor(self, analytics_snapshot: dict[str, Any]) -> dict[str, Any]:
        """运行 AI 顾问: 输入行情/订单/绩效, 输出参数建议"""
        if not self.ai_advisor.enabled:
            return {}

        symbol = self.symbols[0] if self.symbols else "UNKNOWN"
        a = analytics_snapshot.get("symbols", {}).get(symbol, {})

        pos = None
        if self.position_provider:
            p = self.position_provider(symbol)
            if p and p[0] > 0:
                pos = {"quantity": p[0], "avg_price": p[1], "peak_price": p[2]}

        recent_orders = await self._load_recent_orders(symbol)
        performance = await self._load_strategy_performance()

        advice = await self.ai_advisor.advise(
            market_snapshot=a or analytics_snapshot,
            recent_orders=recent_orders,
            strategy_performance=performance,
            position=pos,
        )
        if advice:
            self.logger.info(
                "AI参数建议",
                regime=advice.get("market_regime"),
                grid_spacing=advice.get("grid_spacing"),
                position_ratio=advice.get("position_ratio"),
                risk=advice.get("risk_level"),
                summary=advice.get("summary", "")[:80],
            )
            await self._persist_ai_advice(symbol, advice)
            return {symbol: advice}
        return {}

    async def _load_recent_orders(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """加载近期订单(供 AI 分析)"""
        from sqlalchemy import select

        from common.config.database import AsyncSessionLocal
        from common.models import Order

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(Order)
                            .where(Order.symbol == symbol)
                            .order_by(Order.id.desc())
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                return [
                    {
                        "side": r.side, "price": r.price, "quantity": r.quantity,
                        "status": r.status, "strategy": r.strategy, "is_paper": r.is_paper,
                    }
                    for r in rows
                ]
        except Exception:
            return []

    async def _load_strategy_performance(self) -> list[dict[str, Any]]:
        """加载策略绩效(供 AI 优化)"""
        from sqlalchemy import select

        from common.config.database import AsyncSessionLocal
        from common.models import StrategyPerformance

        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(select(StrategyPerformance))).scalars().all()
                return [
                    {
                        "strategy": r.strategy, "symbol": r.symbol,
                        "trade_count": r.trade_count, "win_rate": round(r.win_rate, 3),
                        "profit": round(r.profit, 2),
                    }
                    for r in rows
                ]
        except Exception:
            return []

    async def _persist_ai_advice(self, symbol: str, advice: dict[str, Any]) -> None:
        """持久化 AI 建议"""
        try:
            from common.config.database import AsyncSessionLocal
            from common.models import AIAdvice

            async with AsyncSessionLocal() as session:
                session.add(
                    AIAdvice(
                        symbol=symbol,
                        advice=advice.get("market_regime", "WATCH"),
                        confidence=0.0,
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
