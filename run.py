"""
adaptiveTrading 主编排器

装配:行情 -> 分析 -> 策略 -> 风控 -> 执行 -> Web
"""

import asyncio
import signal as signal_mod
import sys
import time
from pathlib import Path

# 保证各模块包可导入(atXX 号码分层目录)
ROOT = Path(__file__).parent
for d in (
    "at01_common",
    "at10_web",
    "at20_market",
    "at30_analytics",
    "at40_journal",
    "at50_strategy",
    "at50_execution",
    "at55_portfolio",
    "at60_risk",
    "at70_backtest",
):
    p = ROOT / d
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from at01_common.database import close_db, init_db  # noqa: E402
from at01_common.settings import get_settings  # noqa: E402
from at01_common.logger import get_logger, setup_logging  # noqa: E402


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
        self.portfolio_engine = None  # V3.0: 成本管理
        self.alpha_engine = None  # V3.0: 综合评分
        self.signal_tracker = None  # V3.0: 信号结果跟踪
        self.bucket_manager = None  # V4.0: 核心/交易双仓
        self.allocator = None  # V4.0: 动态敞口
        self.tiered_dd = None  # V4.0: 分级回撤
        self.sizer = None  # V4.0: 评分定仓
        self.reconciler = None  # V8: 持仓对账
        self.startup_reconciler = None  # V10: 启动对账(崩溃窗口恢复)
        self.portfolio_manager = None  # V9.0: 组合编排薄层
        self.core_manager = None  # V9.0: 核心仓低频管理
        self.trading_journal = None  # V9.0: 成交日志
        self.daily_report = None  # V9.0: 每日复盘
        self.strategy_version = None  # V9.0: 策略版本快照
        self.sentiment_analyzer = None  # V9.0 M3.4: 情绪因子(默认关闭)

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
        from at30_analytics.engine import AnalyticsEngine
        from at30_analytics.regime import MarketRegimeEngine
        from at30_analytics.alpha import AlphaEngine
        from at50_execution.execution_executor import ExecutionEngine
        from at20_market.market_engine import MarketDataEngine
        from at60_risk.risk_manager import RiskManager
        from at60_risk.risk_portfolio import PortfolioEngine
        from at60_risk.risk_buckets import BucketPositionManager
        from at60_risk.risk_allocation import PortfolioAllocator
        from at60_risk.risk_tiered import TieredDrawdownManager
        from at60_risk.risk_sizing import PositionSizer
        from at50_strategy.strategy_engine import StrategyEngine
        from at50_strategy.strategy_signal_tracker import SignalResultTracker
        from at10_web import system_state

        # 主网实盘安全守卫(需显式确认, 防误配直接上主网)
        if not self.settings.binance_testnet and self.settings.live_trading_confirm.lower() != "true":
            self.logger.error("拒绝主网实盘启动: 未显式设置 LIVE_TRADING_CONFIRM=true")
            raise RuntimeError("主网实盘需显式确认 LIVE_TRADING_CONFIRM=true 后启动")

        # 风控
        self.risk_manager = RiskManager()
        await self.risk_manager.positions.load_from_db()
        # V10: 恢复急停状态(重启后仍保持冻结, 不自动复位)
        await self.risk_manager.kill_switch.load_from_db()

        # V3.0: Portfolio Engine(成本管理) —— 必须先于执行引擎创建(执行引擎据此记账)
        self.portfolio_engine = PortfolioEngine(self.risk_manager.positions)

        # 执行(依赖风控/组合引擎与 REST)
        self.execution_engine = ExecutionEngine(
            risk_manager=self.risk_manager,
            on_fill=self._on_fill,
            portfolio=self.portfolio_engine,  # V3.0: 成本管理
        )
        # V3.0: 执行的信号注册到结果跟踪器
        self.execution_engine.on_signal_registered = self._register_tracked_signal

        # V10.3: 恢复开仓 lot(FIFO 批次追踪, 崩溃后不丢批次)
        await self.execution_engine.lot_tracker.load_from_db()

        # V8: 交易状态机恢复 + 与持仓对账(有持仓但状态丢失 -> HOLDING)
        await self.execution_engine.trade_sm.load_from_db()
        held = {s for s, p in self.risk_manager.positions.positions.items() if p.quantity > 0}
        self.execution_engine.trade_sm.reconcile_with_positions(held)

        # V8: 纸面现金恢复(重启后纸面资金不重置)
        if self.execution_engine.is_paper:
            await self.execution_engine.paper.load_cash_from_db()

        # V3.0: Alpha Engine(综合评分)
        self.alpha_engine = AlphaEngine()
        # V3.0: 信号结果跟踪
        self.signal_tracker = SignalResultTracker()
        await self.signal_tracker.load_open_from_db()

        # V4.0: 双仓/分配/定仓/分级回撤
        self.bucket_manager = BucketPositionManager(self.risk_manager.positions)
        await self.bucket_manager.load_from_db()
        self.allocator = PortfolioAllocator(initial_equity=self.settings.risk_initial_equity)
        self.tiered_dd = TieredDrawdownManager(hard_breaker=self.risk_manager.breaker)
        self.sizer = PositionSizer()

        # V9.0: 组合编排层(核心/交易/现金三桶) + 记忆层(日志/版本/复盘)
        from at55_portfolio.portfolio_manager import PortfolioManager
        from at55_portfolio.core_manager import CoreAction, CorePositionManager
        from at40_journal.trading_journal import TradingJournal
        from at40_journal.daily_report import DailyReport
        from at50_strategy.strategy_version import StrategyVersionManager

        self.portfolio_manager = PortfolioManager(self.risk_manager.positions, self.bucket_manager)
        self.core_manager = CorePositionManager(self.bucket_manager)
        self.trading_journal = TradingJournal()
        self.daily_report = DailyReport()
        self.strategy_version = StrategyVersionManager()
        # 成交闭环 -> 日志
        self.execution_engine.on_trade_record = self.trading_journal.record

        # V9.0 M3.4: 情绪因子(Funding+OI, 默认关闭; 不碰现货主链路)
        if self.settings.sentiment_enabled:
            from at20_market.market_futures_client import BinanceFuturesClient
            from at30_analytics.sentiment import SentimentAnalyzer

            futures_client = BinanceFuturesClient()
            await futures_client.connect()
            self.sentiment_analyzer = SentimentAnalyzer(client=futures_client)
            self.logger.info("情绪因子已启用(合约 Funding+OI)")

        # 策略
        self.strategy_engine = StrategyEngine(symbols=self.settings.symbol_list, on_signal=self._on_signal)
        self.strategy_engine.position_provider = self._position_provider
        self.strategy_engine.decision_context = self._decision_context  # V4.0
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

        # V8: 持仓对账器(仅实盘接 REST; 纸面做现金自检)
        from at50_execution.reconciliation import PositionReconciler

        self.reconciler = PositionReconciler(
            rest_client=None if self.execution_engine.is_paper else self.market_engine.rest,
        )

        # V10.4: 三维交叉对账(Order/Fill/Ledger/Lot 一致性, 纯 DB 读, 无需 REST)
        from at50_execution.cross_reconciler import CrossReconciler

        self.cross_reconciler = CrossReconciler()

        # V10.7: 订单恢复引擎(仅实盘; UNKNOWN/RECOVERY_REQUIRED 周期收敛 + 账务重建)
        from at50_execution.order_recovery import OrderRecoveryEngine

        self.order_recovery = OrderRecoveryEngine(
            rest_client=None if self.execution_engine.is_paper else self.market_engine.rest,
            execution_engine=self.execution_engine,
            risk_manager=self.risk_manager,
        )

        # V10: 启动对账(仅实盘 + 启用): 崩溃窗口恢复 + 未解决差异 -> 急停冻结
        if not self.execution_engine.is_paper and self.settings.startup_reconcile_enabled:
            from at50_execution.startup_reconciler import StartupReconciler

            self.startup_reconciler = StartupReconciler(
                rest_client=self.market_engine.rest,
                execution_engine=self.execution_engine,
                risk_manager=self.risk_manager,
            )
            for symbol in self.settings.symbol_list:
                diffs = await self.startup_reconciler.reconcile(symbol)
                if diffs:
                    summary = "; ".join(
                        f"{d.get('type')}:{d.get('exchange_order_id') or d.get('client_order_id') or d.get('symbol', '*')}"
                        for d in diffs
                    )
                    self.risk_manager.kill_switch.arm(f"启动对账未通过: {summary}")
                    self.logger.error("启动对账未通过, 已冻结交易", symbol=symbol, diffs=diffs)
            await self.risk_manager.kill_switch.persist()

        # V8: 行情数据异常回调 -> 暂停交易
        self.market_engine.on_data_anomaly = self._on_data_anomaly

        # 注册 Web 状态
        system_state.market_engine = self.market_engine
        system_state.analytics_engine = self.analytics_engine
        system_state.strategy_engine = self.strategy_engine
        system_state.risk_manager = self.risk_manager
        system_state.execution_engine = self.execution_engine
        system_state.regime_engine = self.regime_engine
        system_state.running = True
        system_state.started_at = time.time()

        # V9.0: 启动基线版本快照(每日一份, 同版本去重)
        from datetime import datetime, timezone

        baseline = f"{self.settings.app_version}-{datetime.now(tz=timezone.utc).strftime('%Y%m%d')}"
        await self.strategy_version.snapshot(baseline, note="startup baseline")

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
        # V3.0: 信号结果跟踪(每分钟)
        self._tasks.append(
            asyncio.create_task(self._signal_tracker_loop(), name="signal-tracker-loop")
        )
        # 周期任务:AI 顾问
        if self.strategy_engine.ai_advisor.enabled:
            self._tasks.append(
                asyncio.create_task(self._ai_loop(), name="ai-loop")
            )
        # V8: 持仓对账循环
        self._tasks.append(
            asyncio.create_task(self._reconcile_loop(), name="reconcile-loop")
        )
        # V9.0: 组合再平衡(核心仓低频决策)
        self._tasks.append(
            asyncio.create_task(self._portfolio_loop(), name="portfolio-loop")
        )
        # V9.0: 每日自动复盘
        if self.settings.daily_report_enabled:
            self._tasks.append(
                asyncio.create_task(self._daily_report_loop(), name="daily-report-loop")
            )
        # V9.0 M3.4: 情绪因子低频轮询(默认关闭)
        if self.settings.sentiment_enabled and self.sentiment_analyzer is not None:
            self._tasks.append(
                asyncio.create_task(self._sentiment_loop(), name="sentiment-loop")
            )
        # Web API
        from at10_web.web_app import start_server

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
        if self.sentiment_analyzer is not None and self.sentiment_analyzer.client is not None:
            await self.sentiment_analyzer.client.disconnect()
        await close_db()
        self.logger.info("系统已停止")

    # ---------- 数据管道 ----------

    async def _on_trade(self, symbol: str, tick) -> None:
        """行情 -> 分析(V2.0: 价格异常检测 / V3.0: 24h 注入)"""
        try:
            # V2.0: 价格瞬间波动检测(异常保护)
            self.risk_manager.check_tick_anomaly(symbol, tick.price)
            # V8: 快速暴跌检测(短窗口跌幅超阈值 -> 暂停交易)
            self.risk_manager.check_fast_crash(symbol, tick.price)
            # 更新峰值价(移动止盈)
            self.risk_manager.positions.update_price(symbol, tick.price)
            await self.analytics_engine.on_trade(symbol, tick)
        except Exception:
            self.logger.exception("行情管道异常")

    def _sync_change_24h(self) -> None:
        """V3.0: 行情引擎 24h 涨跌幅 -> 分析引擎(情绪因子)"""
        for symbol, st in self.market_engine.state.items():
            self.analytics_engine.set_change_24h(symbol, st.mark_change_pct_24h)

    async def _on_analytics(self, symbol: str, analytics) -> None:
        """分析 -> 策略"""
        try:
            await self.strategy_engine.on_analytics(symbol, analytics)
        except Exception:
            self.logger.exception("分析管道异常")

    async def _on_signal(self, sig) -> None:
        """策略 -> 风控 -> 执行(V4: 评分定仓)"""
        try:
            # V9.0: 方向闸门(熔断/异常保护)短路; 仅减仓态放行卖出
            if sig.side.value == "BUY":
                gate = self.risk_manager.can_buy()
            else:
                gate = self.risk_manager.can_sell()
            if not gate:
                self.logger.info("信号被交易闸门拦截", symbol=sig.symbol, side=sig.side.value)
                return

            last_prices = {
                s: st.last_price for s, st in self.market_engine.state.items()
            }

            # V4.0: 买入信号经 PositionSizer 评分定仓(替代固定金额)
            if sig.side.value == "BUY":
                a = self.analytics_engine.get(sig.symbol)
                price = last_prices.get(sig.symbol, sig.price)
                equity = self.risk_manager.equity(last_prices)
                # Alpha(机会质量)
                alpha_score = 60.0
                if a is not None:
                    alpha_score = self.alpha_engine.score(a).score
                # regime
                regime = a.regime if a is not None else "SIDEWAY"
                confidence = 0.5
                assessment = self.regime_engine.get(sig.symbol)
                if assessment is not None:
                    regime = assessment.regime
                    confidence = assessment.confidence
                # 敞口缺口(分配引擎)
                plan = self.allocator.plan(
                    symbol=sig.symbol, regime=regime, confidence=confidence,
                    equity=equity, market_price=price,
                    current_core_qty=self.bucket_manager.core(sig.symbol),
                    current_trade_qty=self.bucket_manager.trade(sig.symbol),
                )
                exposure_room = max(0.0, equity * plan.target_exposure - (
                    self.bucket_manager.total(sig.symbol) * price
                ))
                sizing = self.sizer.size(
                    decision_score=sig.score,
                    alpha_score=alpha_score,
                    regime=plan.regime,
                    equity=equity,
                    price=price,
                    tiered_factor=self.tiered_dd.size_factor,
                    exposure_room_quote=exposure_room,
                )
                if sizing["quote"] <= 0:
                    self.logger.info("V4定仓拒绝", symbol=sig.symbol, detail=sizing["detail"])
                    return
                sig.quantity = sizing["quantity"]
                sig.quote_amount = sizing["quote"]
                self.logger.info(
                    "V4评分定仓", symbol=sig.symbol,
                    quote=round(sizing["quote"], 2),
                    ratio=sizing["position_ratio"],
                    alpha=round(alpha_score, 1), regime=plan.regime,
                    tier=self.tiered_dd.current_level,
                )

            decision = await self.risk_manager.check(sig, last_prices)
            if not decision.approved:
                return

            # V5: 卖出数量以交易仓可用量封顶(下单前)
            if sig.side.value == "SELL":
                trade_available = self.bucket_manager.trade(sig.symbol)
                if sig.bucket == "trade" and decision.quantity > trade_available:
                    if trade_available <= 0:
                        self.logger.info(
                            "V5卖出闸门: 交易仓为空, 丢弃", symbol=sig.symbol,
                        )
                        return
                    self.logger.warning(
                        "V5卖出闸门: 缩量至交易仓",
                        symbol=sig.symbol,
                        original=decision.quantity, capped=trade_available,
                    )
                    decision.quantity = trade_available
                    sig.quantity = trade_available

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
                # V4: 双仓记账(仅成交>0; 覆盖 FILLED 与 PARTIALLY_FILLED)
                if result.get("fill_qty", 0.0) > 0:
                    bucket = "trade"  # 策略信号默认入交易仓
                    fill_qty = result.get("fill_qty", 0.0)
                    fill_price = result.get("fill_price", sig.price)
                    if sig.side.value == "BUY":
                        self.bucket_manager.on_buy_fill(sig.symbol, fill_qty, fill_price, bucket)
                    else:
                        realized, used = self.bucket_manager.on_sell_fill(
                            sig.symbol, fill_qty, fill_price, bucket
                        )
                        if used == "REJECTED":
                            # V5 闸门已保证 fill_qty <= 交易仓, 此分支仅防御性兜底;
                            # 真若发生, 差异交由 PositionReconciler 对账检出
                            self.logger.error(
                                "交易仓不足(理论不可达), 待对账", symbol=sig.symbol, qty=fill_qty,
                            )
                    await self.bucket_manager.persist(sig.symbol)
        except Exception:
            self.logger.exception("信号管道异常")

    def _register_tracked_signal(self, signal_id: int, sig) -> None:
        """V3.0: 执行信号 -> 结果跟踪"""
        self.signal_tracker.register(
            signal_id, sig.symbol, sig.strategy, sig.side.value, sig.price
        )

    async def _on_fill(self, sig, fill_price: float, fill_qty: float) -> None:
        """成交回调 -> 策略"""
        try:
            await self.strategy_engine.on_fill(sig, fill_price, fill_qty)
            # 广播到 Web
            from at10_web import broadcast

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

    def _decision_context(self) -> dict:
        """V4.0: 决策日志上下文"""
        last_prices = {
            s: st.last_price for s, st in self.market_engine.state.items()
        }
        symbol = self.settings.symbol_list[0] if self.settings.symbol_list else ""
        a = self.analytics_engine.get(symbol)
        assessment = self.regime_engine.get(symbol) if self.regime_engine else None
        alpha = self.alpha_engine.score(a).score if (a and self.alpha_engine) else 0.0
        equity = self.risk_manager.equity(last_prices)
        return {
            "regime": assessment.regime if assessment else (a.regime if a else ""),
            "regime_confidence": assessment.confidence if assessment else 0.0,
            "alpha_score": alpha,
            "core_qty": self.bucket_manager.core(symbol),
            "trade_qty": self.bucket_manager.trade(symbol),
            "cash": self.execution_engine.paper.cash if self.execution_engine else 0.0,
            "equity": equity,
        }

    def _position_provider(self, symbol: str):
        """供策略查询持仓(V5: 只暴露交易仓——卖出策略不可见核心仓)"""
        trade_qty = self.bucket_manager.trade(symbol)
        if trade_qty <= 0:
            return None
        total = self.risk_manager.positions.get(symbol)
        return (trade_qty, total.avg_price, total.peak_price)

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
                # V4.0: 分级回撤评估(10/20/30/40/50% 五档)
                tier = self.tiered_dd.evaluate(status.get("drawdown", 0.0))
                if tier is not None:
                    self._record_tier_event(tier)
                # V2.0: 行情静默检测
                self.risk_manager.check_market_silence()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("风控循环异常")
            await asyncio.sleep(5)

    async def _record_tier_event(self, tier) -> None:
        """V4: 回撤档位事件落库"""
        try:
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import RiskEvent

            async with AsyncSessionLocal() as session:
                session.add(RiskEvent(
                    event_type="drawdown_tier",
                    detail=f"L{tier.level} {tier.name}: {tier.action}",
                    equity=self.risk_manager.current_equity,
                ))
                await session.commit()
        except Exception:
            self.logger.exception("分级事件落库失败")

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

    async def _signal_tracker_loop(self) -> None:
        """V3.0: 每分钟更新信号未来收益(signal_result 表)"""
        while self._running:
            try:
                last_prices = {
                    s: st.last_price for s, st in self.market_engine.state.items()
                }
                if last_prices:
                    n = await self.signal_tracker.update(last_prices)
                    if n:
                        self.logger.debug("信号跟踪更新", signals=n)
                self._sync_change_24h()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("信号跟踪循环异常")
            await asyncio.sleep(60)

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

    async def _on_data_anomaly(self, symbol: str, issues: list[str]) -> None:
        """行情数据异常 -> 暂停交易"""
        self.risk_manager.pause(f"行情数据异常 {symbol}: {';'.join(issues)}")

    async def _reconcile_loop(self) -> None:
        """V8: 周期对账(实盘: 本地 vs 交易所; 纸面: 现金自检)"""
        while self._running:
            try:
                if self.execution_engine.is_paper:
                    for it in self.reconciler.reconcile_paper(self.execution_engine.paper.cash):
                        self.risk_manager.pause(f"纸面现金异常 {it.get('cash')}")
                else:
                    # V10.7: 订单恢复引擎(先收敛 UNKNOWN/RECOVERY_REQUIRED, 再对账,
                    # 避免「交易所已成交但本地仍 UNKNOWN」被误判为持仓漂移)
                    for rd in await self.order_recovery.recover(self.settings.symbol_list[0]):
                        if rd.get("type") == "recover_error":
                            self.logger.warning("订单恢复异常", detail=rd.get("detail"))
                            continue
                        self.logger.error("订单恢复未收敛", **rd)
                        self.risk_manager.kill_switch.arm(
                            f"订单恢复未收敛 {rd.get('client_order_id')}"
                        )
                        await self.risk_manager.kill_switch.persist()
                    mismatches = await self.reconciler.reconcile_live(
                        self.risk_manager.positions.positions
                    )
                    for m in mismatches:
                        if m.get("type") == "api_error":
                            self.logger.warning("对账 API 异常", detail=m.get("detail"))
                            continue
                        if m.get("type") == "exchange_only":
                            # V10.2: 交易所有 / 本地无 -> 严重漂移, 急停冻结(需人工解除)
                            self.logger.error(
                                "对账发现交易所单侧持仓(本地无)", symbol=m.get("symbol"),
                                exchange=m.get("exchange"),
                            )
                            self.risk_manager.kill_switch.arm(
                                f"交易所单侧持仓 {m.get('symbol')} exchange={m.get('exchange'):.4f}"
                            )
                            await self.risk_manager.kill_switch.persist()
                            continue
                        self.logger.error(
                            "持仓对账不一致", symbol=m.get("symbol"),
                            local=m.get("local"), exchange=m.get("exchange"), diff=m.get("diff"),
                        )
                        self.risk_manager.pause(
                            f"持仓对账不一致 {m.get('symbol')} 差 {m.get('diff'):.4f}"
                        )
                    # V10.3: lot 总和对账(开仓 lot 总和 vs 持仓量; 内部一致性破坏 -> 冻结)
                    for _sym, _pos in self.risk_manager.positions.positions.items():
                        lot_diff = self.execution_engine.lot_tracker.reconcile(_sym, _pos.quantity)
                        if lot_diff is not None:
                            self.logger.error("lot 总和对不上", **lot_diff)
                            self.risk_manager.kill_switch.arm(
                                f"lot 总和对不上 {_sym} 差 {lot_diff['diff']:.6f}"
                            )
                            await self.risk_manager.kill_switch.persist()
                    # V10.4: 三维交叉对账(Order/Fill/Ledger/Lot 内部一致性破坏 -> 冻结)
                    symbol = self.settings.symbol_list[0]
                    for d in await self.cross_reconciler.reconcile(symbol):
                        if d.get("type") == "cross_reconcile_error":
                            self.logger.warning("交叉对账异常", detail=d.get("detail"))
                            continue
                        self.logger.error("交叉对账失败", **d)
                        self.risk_manager.kill_switch.arm(
                            f"交叉对账失败 {d.get('type')} {d.get('client_order_id')}"
                        )
                        await self.risk_manager.kill_switch.persist()
                    # V10: 权益对账(本地 vs 交易所, 超容差 -> 急停冻结, 非 60s pause)
                    last_price = (
                        self.market_engine.state[symbol].last_price
                        if symbol in self.market_engine.state else 0.0
                    )
                    drifts = await self.reconciler.reconcile_account(
                        symbol,
                        self.risk_manager.current_equity,
                        last_price,
                        tolerance_pct=self.settings.equity_reconcile_tolerance_pct,
                    )
                    for dr in drifts:
                        if dr.get("type") == "api_error":
                            self.logger.warning("权益对账 API 异常", detail=dr.get("detail"))
                            continue
                        self.risk_manager.kill_switch.arm(
                            f"权益漂移 local={dr.get('local'):.2f} "
                            f"exchange={dr.get('exchange'):.2f} diff={dr.get('diff'):.2f}"
                        )
                        await self.risk_manager.kill_switch.persist()
                        self.logger.error("权益对账漂移, 已冻结交易", **dr)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("对账循环异常")
            await asyncio.sleep(self.settings.reconcile_interval_seconds)

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

    # ---------- V9.0: 组合再平衡与每日复盘 ----------

    async def _portfolio_loop(self) -> None:
        """V9.0: 低频核心仓决策(ADD/REDUCE/HOLD) + 目标落库"""
        while self._running:
            try:
                last_prices = {
                    s: st.last_price for s, st in self.market_engine.state.items()
                }
                if not last_prices:
                    await asyncio.sleep(self.settings.portfolio_rebalance_interval_seconds)
                    continue
                symbol = self.settings.symbol_list[0]
                price = last_prices.get(symbol, 0.0)
                if price <= 0:
                    await asyncio.sleep(self.settings.portfolio_rebalance_interval_seconds)
                    continue

                equity = self.risk_manager.equity(last_prices)
                analytics = self.analytics_engine.get(symbol)
                assessment = self.regime_engine.get(symbol) if self.regime_engine else None

                # BTC 24h 涨跌幅(供 BTC 锚失败判断)
                btc_change = 0.0
                btc_state = self.market_engine.state.get("BTCUSDT")
                if btc_state is not None:
                    btc_change = btc_state.mark_change_pct_24h

                target_core = self.portfolio_manager.target_core_qty(equity, price)
                decision = self.core_manager.decide(
                    symbol, analytics, assessment, price, target_core, btc_change
                )
                self.logger.info(
                    "核心仓决策", symbol=symbol, action=decision["action"].value,
                    reason=decision["reason"],
                )
                await self._apply_core_action(symbol, price, decision)
                await self.portfolio_manager.persist_targets(symbol, equity, price)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("组合循环异常")
            await asyncio.sleep(self.settings.portfolio_rebalance_interval_seconds)

    async def _apply_core_action(self, symbol: str, price: float, decision: dict) -> None:
        """V9.0: 执行核心仓 ADD/REDUCE(经执行引擎, 数量已由组合层决定)"""
        action = decision["action"]
        if action.value not in ("ADD", "REDUCE"):
            return
        # 加仓受买入闸门约束; 减仓(Trend Break Protection 保护性退出)始终放行
        if action == CoreAction.ADD and not self.risk_manager.can_buy():
            self.logger.info("核心仓加仓被闸门拦截", action=action.value)
            return
        qty = decision.get("add_qty") or decision.get("reduce_qty") or 0.0
        if qty <= 0:
            return

        from at50_strategy.strategy_base import Signal, SignalSide

        side = SignalSide.BUY if action.value == "ADD" else SignalSide.SELL
        sig = Signal(
            symbol=symbol, strategy="core_manager", side=side, price=price,
            quantity=qty, quote_amount=qty * price, reason=[decision["reason"]],
            score=100.0, bucket="core",
        )
        result = await self.execution_engine.execute(sig)
        if result and result.get("fill_qty", 0.0) > 0:
            fill_qty = result.get("fill_qty", 0.0)
            fill_price = result.get("fill_price", price)
            if side == SignalSide.BUY:
                self.bucket_manager.on_buy_fill(symbol, fill_qty, fill_price, "core")
            else:
                self.bucket_manager.on_sell_fill(symbol, fill_qty, fill_price, "core")
            await self.bucket_manager.persist(symbol)

    async def _daily_report_loop(self) -> None:
        """V9.0: 每日复盘报告"""
        while self._running:
            try:
                symbol = self.settings.symbol_list[0]
                last_prices = {
                    s: st.last_price for s, st in self.market_engine.state.items()
                }
                equity = self.risk_manager.equity(last_prices)
                assessment = self.regime_engine.get(symbol) if self.regime_engine else None
                regime = assessment.regime if assessment else ""
                await self.daily_report.generate(symbol, regime=regime, equity=equity)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("每日复盘循环异常")
            await asyncio.sleep(86400)

    async def _sentiment_loop(self) -> None:
        """V9.0 M3.4: 情绪因子低频轮询(仅 sentiment_enabled 时启动)"""
        while self._running:
            try:
                symbol = self.settings.symbol_list[0]
                result = await self.sentiment_analyzer.poll(symbol)
                if result is not None:
                    self.logger.info("情绪因子更新", symbol=symbol, **result.to_dict())
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("情绪因子循环异常")
            await asyncio.sleep(self.settings.sentiment_poll_interval_seconds)


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
