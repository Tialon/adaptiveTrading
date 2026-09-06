"""
adaptiveTrading 主编排器

装配:行情 -> 分析 -> 策略 -> 风控 -> 执行 -> Web
"""

import asyncio
import signal as signal_mod
import sys
import time
from pathlib import Path

# 保证各模块包可导入(目录带数字前缀)
ROOT = Path(__file__).parent
for d in ("00_common", "10_web", "20_market", "30_ayalytics", "50_startegy", "50_execution", "60_risk", "70_backtest"):
    p = ROOT / d
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from common.config.database import close_db, init_db  # noqa: E402
from common.config.settings import get_settings  # noqa: E402
from common.utils.logger import get_logger, setup_logging  # noqa: E402


class AdaptiveTradingSystem:
    """系统主类"""

    def __init__(self):
        self.settings = get_settings()
        self.logger = get_logger("System")
        self._running = False
        self._tasks: list[asyncio.Task] = []

        self.market_engine = None
        self.analytics_engine = None
        self.strategy_engine = None
        self.risk_manager = None
        self.execution_engine = None
        self.regime_engine = None

    async def initialize(self) -> None:
        """装配各引擎"""
        setup_logging()
        self.logger.info(
            "初始化",
            app=self.settings.app_name,
            version=self.settings.app_version,
            symbols=self.settings.symbol_list,
            paper=self.settings.paper_trading,
        )

        await init_db()

        # 延迟导入(确保 sys.path 已注入)
        from analytics.engine import AnalyticsEngine
        from analytics.regime import MarketRegimeEngine
        from execution.executor import ExecutionEngine
        from market.engine import MarketDataEngine
        from risk.manager import RiskManager
        from strategy.engine import StrategyEngine
        from web.state import system_state

        # 风控
        self.risk_manager = RiskManager()
        await self.risk_manager.positions.load_from_db()

        # 执行(依赖风控与 REST)
        self.execution_engine = ExecutionEngine(
            risk_manager=self.risk_manager,
            on_fill=self._on_fill,
        )

        # 策略
        self.strategy_engine = StrategyEngine(symbols=self.settings.symbol_list, on_signal=self._on_signal)
        self.strategy_engine.position_provider = self._position_provider
        self.strategy_engine.setup()

        # 分析
        self.analytics_engine = AnalyticsEngine(
            symbols=self.settings.symbol_list,
            on_analytics=self._on_analytics,
        )

        # 行情
        self.market_engine = MarketDataEngine(
            symbols=self.settings.symbol_list,
            on_trade=self._on_trade,
        )
        await self.market_engine.start()

        # V2.0: Market Regime Engine
        self.regime_engine = MarketRegimeEngine(
            watch_interval=self.settings.regime_watch_interval
        )

        # 注入 REST 客户端供实盘执行
        self.execution_engine.rest = self.market_engine.rest

        # 注册 Web 状态
        system_state.market_engine = self.market_engine
        system_state.analytics_engine = self.analytics_engine
        system_state.strategy_engine = self.strategy_engine
        system_state.risk_manager = self.risk_manager
        system_state.execution_engine = self.execution_engine
        system_state.regime_engine = self.regime_engine
        system_state.running = True
        system_state.started_at = time.time()

        self.logger.info("系统初始化完成")

    async def start(self) -> None:
        """启动后台任务"""
        self._running = True

        # 周期任务:风控权益更新
        self._tasks.append(
            asyncio.create_task(self._risk_loop(), name="risk-loop")
        )
        # V2.0: Market Regime 评估
        if self.settings.regime_enabled:
            self._tasks.append(
                asyncio.create_task(self._regime_loop(), name="regime-loop")
            )
        # V2.0: 持仓快照(收益曲线)
        self._tasks.append(
            asyncio.create_task(self._snapshot_loop(), name="snapshot-loop")
        )
        # 周期任务:AI 顾问
        if self.strategy_engine.ai_advisor.enabled:
            self._tasks.append(
                asyncio.create_task(self._ai_loop(), name="ai-loop")
            )
        # Web API
        from web.app import start_server

        self._tasks.append(
            asyncio.create_task(
                start_server(self.settings.api_host, self.settings.api_port),
                name="web-server",
            )
        )

        self.logger.info(
            "系统已启动", api=f"http://{self.settings.api_host}:{self.settings.api_port}"
        )
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        """优雅停机"""
        if not self._running and not self.market_engine:
            return
        self._running = False
        self.logger.info("正在停止…")

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        if self.market_engine:
            await self.market_engine.stop()
        if self.strategy_engine:
            await self.strategy_engine.close()
        await close_db()
        self.logger.info("系统已停止")

    # ---------- 数据管道 ----------

    async def _on_trade(self, symbol: str, tick) -> None:
        """行情 -> 分析(V2.0: 价格异常检测)"""
        try:
            # V2.0: 价格瞬间波动检测(异常保护)
            self.risk_manager.check_tick_anomaly(symbol, tick.price)
            # 更新峰值价(移动止盈)
            self.risk_manager.positions.update_price(symbol, tick.price)
            await self.analytics_engine.on_trade(symbol, tick)
        except Exception:
            self.logger.exception("行情管道异常")

    async def _on_analytics(self, symbol: str, analytics) -> None:
        """分析 -> 策略"""
        try:
            await self.strategy_engine.on_analytics(symbol, analytics)
        except Exception:
            self.logger.exception("分析管道异常")

    async def _on_signal(self, sig) -> None:
        """策略 -> 风控 -> 执行"""
        try:
            last_prices = {
                s: st.last_price for s, st in self.market_engine.state.items()
            }
            decision = await self.risk_manager.check(sig, last_prices)
            if not decision.approved:
                return

            sig.quantity = decision.quantity
            sig.price = decision.price
            result = await self.execution_engine.execute(sig)
            if result:
                self.logger.info(
                    "订单完成",
                    symbol=sig.symbol,
                    status=result["status"],
                    fill_qty=result.get("fill_qty"),
                    fill_price=result.get("fill_price"),
                )
        except Exception:
            self.logger.exception("信号管道异常")

    async def _on_fill(self, sig, fill_price: float, fill_qty: float) -> None:
        """成交回调 -> 策略"""
        try:
            await self.strategy_engine.on_fill(sig, fill_price, fill_qty)
            # 广播到 Web
            from web.app import broadcast

            await broadcast(
                {
                    "type": "fill",
                    "symbol": sig.symbol,
                    "side": sig.side.value,
                    "price": fill_price,
                    "quantity": fill_qty,
                    "strategy": sig.strategy,
                }
            )
        except Exception:
            self.logger.exception("成交回调异常")

    def _position_provider(self, symbol: str):
        """供策略查询持仓"""
        pos = self.risk_manager.positions.get_or_none(symbol)
        if pos is None:
            return None
        return (pos.quantity, pos.avg_price, pos.peak_price)

    # ---------- 周期任务 ----------

    async def _risk_loop(self) -> None:
        """每 5 秒更新权益/回撤/熔断 + 行情静默检测"""
        while self._running:
            try:
                last_prices = {
                    s: st.last_price for s, st in self.market_engine.state.items()
                }
                status = self.risk_manager.update_equity(last_prices)
                if status.get("breaker_open"):
                    self.logger.warning(
                        "熔断生效中", reason=self.risk_manager.breaker.reason
                    )
                # V2.0: 行情静默检测
                self.risk_manager.check_market_silence()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("风控循环异常")
            await asyncio.sleep(5)

    async def _regime_loop(self) -> None:
        """V2.0: 周期评估市场环境,注入分析引擎"""
        while self._running:
            try:
                # BTC 锚(若未订阅 BTC, 用 24h 涨跌幅近似)
                btc_trend = "neutral"
                btc_change = 0.0
                btc_state = self.market_engine.state.get("BTCUSDT")
                if btc_state is not None:
                    btc_trend = "up" if btc_state.mark_change_pct_24h > 2 else (
                        "down" if btc_state.mark_change_pct_24h < -2 else "neutral"
                    )
                    btc_change = btc_state.mark_change_pct_24h

                for symbol in self.settings.symbol_list:
                    a = self.analytics_engine.get(symbol)
                    if a is None:
                        continue
                    assessment = self.regime_engine.evaluate(
                        symbol=symbol,
                        symbol_trend=a.trend,
                        symbol_ema_fast=a.ema_fast,
                        symbol_ema_slow=a.ema_slow,
                        recent_high=a.recent_high,
                        recent_low=a.recent_low,
                        volume_ratio=a.volume_ratio,
                        delta_ratio=a.delta_ratio,
                        cvd_rising=a.cvd_rising,
                        btc_trend=btc_trend,
                        btc_change_24h=btc_change,
                    )
                    # 注入分析快照(策略可读)
                    self.analytics_engine.set_regime(symbol, assessment.regime)
                    self.logger.info(
                        "市场环境", symbol=symbol, regime=assessment.regime,
                        confidence=assessment.confidence,
                        reasons=";".join(assessment.reasons or []),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("市场环境评估异常")
            await asyncio.sleep(self.settings.regime_watch_interval)

    async def _snapshot_loop(self) -> None:
        """V2.0: 每 60 秒持仓快照落库(收益曲线)"""
        while self._running:
            try:
                last_prices = {
                    s: st.last_price for s, st in self.market_engine.state.items()
                }
                equity = self.risk_manager.equity(last_prices)
                for symbol, price in last_prices.items():
                    if price > 0:
                        await self.risk_manager.positions.snapshot_to_db(symbol, price, equity)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("持仓快照异常")
            await asyncio.sleep(60)

    async def _ai_loop(self) -> None:
        """AI 顾问周期分析"""
        while self._running:
            try:
                snapshot = self.analytics_engine.snapshot()
                await self.strategy_engine.run_ai_advisor(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("AI 循环异常")
            await asyncio.sleep(self.settings.ai_interval_seconds)


async def main() -> None:
    """入口"""
    system = AdaptiveTradingSystem()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows
            signal_mod.signal(sig, lambda *_: stop_event.set())

    try:
        await system.initialize()
        server_task = asyncio.create_task(system.start())
        stop_task = asyncio.create_task(stop_event.wait())

        done, _ = await asyncio.wait(
            {server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except KeyboardInterrupt:
        pass
    finally:
        await system.stop()


if __name__ == "__main__":
    asyncio.run(main())
