"""
策略引擎

管理所有策略实例,接收分析快照,聚合各策略信号并回调(风控引擎)。
"""

from typing import Any, Awaitable, Callable, Optional

from at20_analytics.engine import MarketAnalytics
from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at30_strategy.strategy_ai_advisor import AIAdvisor
from at30_strategy.ai_parameter_guard import AIParameterGuard, RuntimeParams, parse_pct
from at30_strategy.strategy_base import BaseStrategy, Signal
from at30_strategy.strategy_decision import DecisionEngine
from at30_strategy.strategy_journal import DecisionJournal
from at30_strategy.strategy_buy import BuyStrategy
from at30_strategy.strategy_grid import GridStrategy
from at30_strategy.strategy_sell import SellStrategy
from at30_strategy.strategy_trend import TrendStrategy

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
        self.ai_param_guard = AIParameterGuard()  # V8: 参数变化审批层
        self.position_provider: Optional[PositionProvider] = None
        self.signal_count = 0
        # V3.0: 多策略融合决策引擎
        self.decision_engine = DecisionEngine()
        # V4.0: 决策日志 + 上下文提供者(由 run.py 注入)
        self.journal = DecisionJournal()
        self.decision_context = None  # callable() -> dict(regime/confidence/alpha/cash/equity/core/trade)

    def setup(self) -> None:
        """按配置装配策略(V6: StrategyType 统一枚举, 兼容旧 buy/sell 配置)"""
        from at30_strategy.strategy_identity import StrategyType

        enabled = set(self.settings.enabled_strategies)
        # 旧配置兼容
        if "buy" in enabled:
            enabled.discard("buy"); enabled.add(StrategyType.ENTRY.value)
        if "sell" in enabled:
            enabled.discard("sell"); enabled.add(StrategyType.EXIT.value)

        if StrategyType.ENTRY.value in enabled:
            self.strategies[StrategyType.ENTRY.value] = BuyStrategy(self.symbols)
        if StrategyType.EXIT.value in enabled:
            sell = SellStrategy(self.symbols)
            sell.position_provider = self.position_provider
            self.strategies[StrategyType.EXIT.value] = sell
        if StrategyType.GRID.value in enabled:
            self.strategies[StrategyType.GRID.value] = GridStrategy(self.symbols)
        if StrategyType.TREND.value in enabled:
            self.strategies[StrategyType.TREND.value] = TrendStrategy(self.symbols)

        self.logger.info("策略装配完成", strategies=list(self.strategies))

    # ---------- 主入口 ----------

    async def on_analytics(self, symbol: str, analytics: MarketAnalytics) -> None:
        """分析引擎回调:运行所有策略生成信号(V2.0: regime 调整 + 标准信号落库)"""
        signals: list[Signal] = []

        # Market Regime 调整(V2.0)
        regime = analytics.regime
        adjustment = None
        if regime:
            from at20_analytics.regime import MarketRegimeEngine

            adjustment = MarketRegimeEngine.strategy_adjustment(regime)

        for strategy in self.strategies.values():
            if not strategy.enabled:
                continue
            # BEAR/PANIC 下禁用网格
            if adjustment and not adjustment.get("grid_enabled", True) and strategy.name == "grid":
                continue
            # PANIC 下禁用一切买入能力(策略能力声明)
            if adjustment and regime == "PANIC":
                from at30_strategy.strategy_identity import capability

                if capability(strategy.name, "can_buy"):
                    continue
            try:
                result = strategy.on_market(analytics)
                if result:
                    signals.extend(result)
            except Exception:
                self.logger.exception("策略执行异常", strategy=strategy.name)

        # V3.0: Decision Engine 融合 -> 唯一动作(或 HOLD)
        decision = self.decision_engine.decide(signals, analytics)
        self.logger.info(
            "融合决策",
            symbol=decision.symbol,
            action=decision.action,
            confidence=decision.confidence,
            net_score=decision.net_score,
            votes=[f"{v['strategy']}:{v['side']}" for v in decision.votes],
        )
        # V4.0: 决策日志(完整上下文, AI 复盘数据)
        try:
            ctx = self.decision_context() if self.decision_context else {}
            await self.journal.log(
                symbol=decision.symbol,
                action=decision.action,
                price=decision.price,
                quantity=decision.quantity or 0.0,
                regime=ctx.get("regime", analytics.regime),
                regime_confidence=ctx.get("regime_confidence", 0.0),
                alpha_score=ctx.get("alpha_score", 0.0),
                decision_score=decision.confidence,
                core_qty=ctx.get("core_qty", 0.0),
                trade_qty=ctx.get("trade_qty", 0.0),
                cash=ctx.get("cash", 0.0),
                equity=ctx.get("equity", 0.0),
                reason="; ".join(decision.reason + [f"votes: {decision.votes}"]),
                context={
                    "indicators": {k: v for k, v in analytics.to_dict().items()
                                   if isinstance(v, (int, float, bool, str))},
                },
            )
        except Exception:
            self.logger.exception("决策日志异常")

        if not decision.actionable:
            # HOLD: 各策略信号仍落库(复盘), 但不下发执行
            for signal in signals:
                self.signal_count += 1
                await self._persist_signal(signal)
            return

        # 唯一动作信号: strategy=decision + source_strategy(回调路由)
        chosen = next(
            (s for s in signals if s.side == decision.side and s.price == decision.price),
            None,
        ) or signals[0]
        fused = Signal(
            symbol=decision.symbol,
            strategy="decision",
            source_strategy=chosen.strategy,  # V6: on_fill 路由回原策略
            side=decision.side,
            price=decision.price,
            quantity=decision.quantity,
            quote_amount=decision.quote_amount,
            reason=decision.reason + [f"融合自{len(signals)}信号: " + "; ".join(
                f"{s.strategy}:{s.side.value}:{s.score:.0f}" for s in signals)],
            score=decision.confidence,
            indicators={
                "decision": decision.to_dict(),
                "source_votes": [
                    {"strategy": s.strategy, "side": s.side.value, "score": s.score}
                    for s in signals
                ],
            },
        )
        self.signal_count += 1
        await self._persist_signal(fused)
        for signal in signals:  # 原始信号也落库(复盘)
            await self._persist_signal(signal)
        if self.on_signal:
            await self.on_signal(fused)

    async def _persist_signal(self, signal: Signal) -> None:
        """标准信号落库(strategy_signal: score/reason/indicators)"""
        import json as _json

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Signal as SignalModel

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
        """通知各策略成交(V6: 融合信号路由回源策略)"""
        from at30_strategy.strategy_identity import StrategyType

        key = signal.source_strategy or signal.strategy
        if key == StrategyType.DECISION.value:
            key = signal.source_strategy
        strategy = self.strategies.get(key)
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
            decisions = self._apply_ai_parameters(symbol, advice)
            await self._record_ai_parameter_history(symbol, advice, decisions)
            return {symbol: advice}
        return {}

    def _apply_ai_parameters(self, symbol: str, advice: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """AI 参数建议 -> 审批层 -> 应用(仅阈值内变化), 返回各参数决策

        原则: AI 只建议, 不交易。超阈值的变化不自动应用(记 history effective=False)。
        """
        baselines = {
            "grid_spacing": RuntimeParams.get("grid_spacing") or self.settings.grid_upper_pct,
            "position_ratio": RuntimeParams.get("position_ratio") or 0.10,
        }
        decisions: dict[str, dict[str, Any]] = {}
        for param in ("grid_spacing", "position_ratio"):
            raw = advice.get(param)
            new_val = parse_pct(raw)
            if new_val is None:
                continue
            current = baselines[param]
            d = self.ai_param_guard.evaluate(param, current, new_val)
            decisions[param] = {
                "applied": d["applied"], "value": d["value"], "reason": d["reason"],
                "old": current, "new": new_val,
            }
            if d["applied"]:
                RuntimeParams.set(param, d["value"])
                self.logger.info(
                    "AI参数已应用", param=param, old=round(current, 4), new=round(d["value"], 4)
                )
            else:
                self.logger.warning("AI参数未应用", param=param, reason=d["reason"])
        return decisions

    async def _record_ai_parameter_history(
        self, symbol: str, advice: dict[str, Any], decisions: Optional[dict[str, dict[str, Any]]] = None
    ) -> None:
        """V5: AI 参数调整历史(与上次建议对比, 含审批结果 effective)"""
        from at01_common.database import AsyncSessionLocal
        from at01_common.models import AIParameterHistory

        decisions = decisions or {}
        tracked = ("grid_spacing", "position_ratio", "risk_level", "market_regime")
        try:
            async with AsyncSessionLocal() as session:
                # 上一条相同参数值
                from sqlalchemy import select

                rows = (
                    await session.execute(
                        select(AIParameterHistory)
                        .where(AIParameterHistory.symbol == symbol)
                        .order_by(AIParameterHistory.id.desc())
                        .limit(20)
                    )
                ).scalars().all()
                last_values = {r.param_name: r.new_value for r in rows}

                for param in tracked:
                    new_val = str(advice.get(param, ""))
                    if not new_val:
                        continue
                    old_val = last_values.get(param)
                    dec = decisions.get(param)
                    effective = dec["applied"] if dec else None
                    if old_val != new_val:  # 变化才记录
                        session.add(
                            AIParameterHistory(
                                symbol=symbol,
                                param_name=param,
                                old_value=old_val,
                                new_value=new_val,
                                reason=advice.get("summary", "")[:1000],
                                effective=effective,
                            )
                        )
                await session.commit()
        except Exception:
            self.logger.exception("AI参数历史落库失败")

    async def _load_recent_orders(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """加载近期订单(供 AI 分析)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order

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

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyPerformance

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
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import AIAdvice

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
