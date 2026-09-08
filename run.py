"""
adaptiveTrading 主编排器 —— 入口壳(V11.6 P2 轻量抽取)

装配(bootstrap/wiring)、运行时(runtime)已抽到 at01_common; 本文件保留:
- `AdaptiveTradingSystem` 类: 交易语义方法(_on_signal/_risk_loop/_reconcile_loop/...)不抽离、
  不微服务化; 仅 `initialize()` 委托给 `at01_common.wiring.wire_system`。
- `__main__` 入口: 委托给 `at01_common.runtime.run`。
"""

import asyncio
import json
import time

from at01_common.bootstrap import inject_sys_path

# 保证各模块包可导入(atXX 号码分层目录), 必须在任何 atXX 包 import 之前执行
inject_sys_path()

from at01_common.database import close_db  # noqa: E402
from at01_common.settings import get_settings  # noqa: E402
from at01_common.logger import get_logger  # noqa: E402
from at01_common.runtime_supervisor import RuntimeSupervisor  # noqa: E402
from at01_common.runtime import run  # noqa: E402


class AdaptiveTradingSystem:
    """系统主类"""

    def __init__(self):
        self.settings = get_settings()
        self.logger = get_logger("System")
        self._running = False
        self._shutting_down = False
        # V11.5 P0-2: 后台任务监督器(创建/命名/状态/异常捕获/critical 异常→安全态/幂等停机)
        self.supervisor = RuntimeSupervisor(
            on_critical_failure=self._handle_critical_task_failure
        )
        # 停机/异常处置中 fire-and-forget 的持久化任务(避免「Task was destroyed but pending」)
        self._pending_tasks: set[asyncio.Task] = set()

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
        self.hodl_benchmark = None  # V12 §24: HODL 基准(接管基线 + 每日对标)
        self.strategy_version = None  # V9.0: 策略版本快照
        self.sentiment_analyzer = None  # V9.0 M3.4: 情绪因子(默认关闭)
        self.lifecycle = None  # V11.1 P1-3: 顶层生命周期状态机
        self.fund_breaker = None  # V11.1 P1-5: 资金级 Circuit Breaker
        self.metrics = None  # V11.1 P1-4: 生产可观测性指标
        self.trading_gate = None  # V11.2 P0-2: 统一交易闸门(单一权威)
        self.last_alerts = []  # V11.2 P1-2: 最近一次指标告警
        self._active_alerts: set[str] = set()  # V11.3 P0-10: 当前生效告警名(降噪: 仅变化时告警)

    async def initialize(self) -> None:
        """装配各引擎(V11.6 P2: 装配逻辑抽到 at01_common.wiring.wire_system)。"""
        from at01_common.wiring import wire_system

        await wire_system(self)

    async def start(self) -> None:
        """启动后台任务(V11.5 P0-2: 由 RuntimeSupervisor 统一创建/命名/跟踪)"""
        self._running = True

        tasks: list[asyncio.Task] = []

        # 周期任务:风控权益更新(critical: 失效即失去风险监控)
        tasks.append(
            self.supervisor.spawn(self._risk_loop(), name="risk-loop", critical=True)
        )
        # V2.0: Market Regime 评估
        if self.settings.regime_enabled:
            tasks.append(
                self.supervisor.spawn(self._regime_loop(), name="regime-loop")
            )
        # V2.0: 持仓快照(收益曲线)
        tasks.append(
            self.supervisor.spawn(self._snapshot_loop(), name="snapshot-loop")
        )
        # V3.0: 信号结果跟踪(每分钟)
        tasks.append(
            self.supervisor.spawn(
                self._signal_tracker_loop(), name="signal-tracker-loop"
            )
        )
        # 周期任务:AI 顾问
        if self.strategy_engine.ai_advisor.enabled:
            tasks.append(
                self.supervisor.spawn(self._ai_loop(), name="ai-loop")
            )
        # V8: 持仓对账循环(critical: 失效即失去漂移检测)
        tasks.append(
            self.supervisor.spawn(self._reconcile_loop(), name="reconcile-loop", critical=True)
        )
        # V9.0: 组合再平衡(核心仓低频决策)
        tasks.append(
            self.supervisor.spawn(self._portfolio_loop(), name="portfolio-loop")
        )
        # V9.0: 每日自动复盘
        if self.settings.daily_report_enabled:
            tasks.append(
                self.supervisor.spawn(self._daily_report_loop(), name="daily-report-loop")
            )
        # V9.0 M3.4: 情绪因子低频轮询(默认关闭)
        if self.settings.sentiment_enabled and self.sentiment_analyzer is not None:
            tasks.append(
                self.supervisor.spawn(self._sentiment_loop(), name="sentiment-loop")
            )
        # Web API
        from at10_web.web_app import start_server

        tasks.append(
            self.supervisor.spawn(
                start_server(self.settings.api_host, self.settings.api_port),
                name="web-server",
            )
        )

        self.logger.info(
            "系统已启动",
            api=f"http://{self.settings.api_host}:{self.settings.api_port}",
            tasks=len(tasks),
        )
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        """优雅停机(V11.5 P0-2: 先置停机标志禁 BUY, 再由监督器幂等回收任务, 后关资源)"""
        if self._shutting_down:
            return
        if not self._running and not self.market_engine:
            return
        # V11.5 P0-2: 停机即禁止一切新开仓(BUY/ADD), 防在途信号在回收窗口内偷偷建仓
        self._shutting_down = True
        # V11.6 P0-2: 同步到统一闸门(单一权威, 与 _on_signal 早退语义一致)
        gate = getattr(self, "trading_gate", None)
        if gate is not None:
            gate.shutting_down = True
        self._running = False
        self.logger.info("正在停止…")

        # 幂等优雅停机: 取消并等待所有后台任务回收
        await self.supervisor.shutdown()

        # fire-and-forget 持久化任务回收
        for task in list(self._pending_tasks):
            task.cancel()
        self._pending_tasks.clear()

        if self.market_engine:
            await self.market_engine.stop()
        if self.strategy_engine:
            await self.strategy_engine.close()
        if self.sentiment_analyzer is not None and self.sentiment_analyzer.client is not None:
            await self.sentiment_analyzer.client.disconnect()
        # V11.3 P0-7: 等待在途风险事件落库(防停机丢审计事件)
        if self.risk_manager:
            await self.risk_manager.flush_events()
        await close_db()
        self.logger.info("系统已停止")

    def _handle_critical_task_failure(self, name: str, exc: BaseException) -> None:
        """V11.5 P0-2: critical 后台任务异常退出 → 进入安全状态(急停 + SAFE_MODE)。

        由 RuntimeSupervisor 在 done 回调里同步调用(不阻塞事件循环), 立即:
        1. arm 急停(内存态, 即刻生效, TradingGate 拒绝一切开仓);
        2. 生命周期进入 SAFE_MODE;
        3. fire-and-forget 持久化急停(重启后仍保持冻结)。
        """
        self.logger.error("critical 后台任务异常退出, 进入安全状态", task=name, error=repr(exc))
        # V11.5 P1-1: 记录最近一次运行时错误(供 runtime health 快照)。
        try:
            from at10_web import system_state

            system_state.last_error = {
                "ts": time.time(), "source": f"critical 任务 {name}", "message": repr(exc),
            }
        except Exception:
            pass
        try:
            if self.risk_manager is not None:
                self.risk_manager.kill_switch.arm(f"critical 任务 {name} 异常退出")
            if self.lifecycle is not None:
                self.lifecycle.enter_safe_mode(f"critical 任务 {name} 异常退出")
            # V11.6 P0-2: 同步到统一闸门(BUY 安全契约: 关键后台任务未运行 → 禁开仓)
            gate = getattr(self, "trading_gate", None)
            if gate is not None:
                gate.critical_tasks_healthy = False
            if self.risk_manager is not None:
                self._pending_tasks.add(
                    asyncio.create_task(self._persist_critical_kill_switch())
                )
        except Exception:
            self.logger.exception("critical 任务安全处置失败", task=name)

    async def _persist_critical_kill_switch(self) -> None:
        """持久化急停(不阻塞监督器回调; 失败仅记日志)。"""
        try:
            await self.risk_manager.kill_switch.persist()
        except Exception:
            self.logger.exception("critical 急停持久化失败")

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
        # V11.5 P0-2: 停机后禁止一切新开仓/减仓(graceful shutdown 停止处理新信号)
        # getattr 兜底: 允许 object.__new__ 构造的最小假系统(无该属性)按「运行中」处理
        if getattr(self, "_shutting_down", False):
            return
        from at50_execution.observability import record_execution

        try:
            # V11.2 P0-2: 统一交易闸门(单一权威, 组合六维); 买走 open, 卖走 reduce
            if sig.side.value == "BUY":
                gate_ok, gate_reason = self.trading_gate.can_open_position()
            else:
                gate_ok, gate_reason = self.trading_gate.can_reduce_position()
            if not gate_ok:
                self.logger.info(
                    "信号被交易闸门拦截",
                    symbol=sig.symbol, side=sig.side.value, reason=gate_reason,
                )
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
            t0 = time.perf_counter()
            result = await self.execution_engine.execute(sig)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            if result:
                self.logger.info(
                    "订单完成",
                    symbol=sig.symbol,
                    status=result["status"],
                    fill_qty=result.get("fill_qty"),
                    fill_price=result.get("fill_price"),
                )
                # V11.2 P1-2: 观测指标(下单尝试总数/失败/UNKNOWN/RECOVERY_REQUIRED/延迟)
                record_execution(
                    self.metrics,
                    status=result["status"],
                    latency_ms=latency_ms,
                    strategy=sig.strategy,
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
                        # V11.2 P1-2: 策略已实现盈亏归因
                        self.metrics.add_strategy_pnl(sig.strategy, realized)
                        if used == "REJECTED":
                            # V5 闸门已保证 fill_qty <= 交易仓, 此分支仅防御性兜底;
                            # 真若发生, 差异交由 PositionReconciler 对账检出
                            self.logger.error(
                                "交易仓不足(理论不可达), 待对账", symbol=sig.symbol, qty=fill_qty,
                            )
                    await self.bucket_manager.persist(sig.symbol)
            else:
                # 执行引擎返回 None(闸门/尺寸/资金不足等拒绝) -> 记一次拒绝尝试
                record_execution(
                    self.metrics, status="REJECTED", latency_ms=latency_ms, strategy=sig.strategy,
                )
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
        from at10_web import system_state
        from at50_execution.observability import evaluate_alerts

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
                # V12 §19: 分级回撤评估(5/8/12/15% 四档)
                tier = self.tiered_dd.evaluate(status.get("drawdown", 0.0))
                if tier is not None:
                    self._record_tier_event(tier)
                    # 12% 档 -> 仅减仓(禁开新仓、保留卖出); 15% 档已由 update_equity 持久急停
                    if tier.level == 3:
                        self.risk_manager.reduce_only(f"回撤达 {tier.name} 档")
                # V2.0: 行情静默检测
                was_silent = self.risk_manager.silence_active
                self.risk_manager.check_market_silence()
                # V11.2 P1-2: 行情数据缺口指标(静默秒数 -> gauge)
                self.metrics.gauge("data_gap_seconds", self.risk_manager.ws_silence_seconds)
                # V11.4 P1-5: data_gaps 计数 —— 仅在「进入静默」的瞬间 +1(修复读而不写的死指标)
                if self.risk_manager.silence_active and not was_silent:
                    self.metrics.incr("data_gaps")
                # V11.2 P0-2: 更新统一闸门健康信号(连接/行情健康)
                self.trading_gate.connection_ok = bool(
                    self.market_engine.ws and self.market_engine.ws.connected
                )
                self.trading_gate.market_data_healthy = any(
                    st.last_price > 0 for st in self.market_engine.state.values()
                )
                # V11.2 P1-2: 阈值告警评估(失败率/漂移/数据缺口/延迟/恢复连续)
                self.last_alerts = evaluate_alerts(self.metrics)
                system_state.last_alerts = self.last_alerts
                # V11.3 P0-10: 告警降噪 —— 仅告警集合变化(新增/解除)时落日志,
                # 避免持续告警每 5s 刷屏填满轮转日志。
                active = {a.name for a in self.last_alerts}
                if active != self._active_alerts:
                    for a in self.last_alerts:
                        if a.name not in self._active_alerts:
                            self.logger.warning(
                                "指标告警", severity=a.severity, name=a.name, message=a.message,
                            )
                    for name in self._active_alerts - active:
                        self.logger.info("指标告警解除", name=name)
                    self._active_alerts = active
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
        """V11.1(P0-5): 周期对账 —— 统一经对账矩阵判定(单一 kill 决策点, 单一对账器不得 kill)"""
        from at50_execution.reconciliation_matrix import ReconciliationMatrix

        while self._running:
            try:
                matrix = ReconciliationMatrix()
                if self.execution_engine.is_paper:
                    matrix.ingest(
                        "position",
                        self.reconciler.reconcile_paper(self.execution_engine.paper.cash),
                    )
                else:
                    symbol = self.settings.symbol_list[0]
                    # V10.7: 先收敛 UNKNOWN/RECOVERY_REQUIRED, 再对账, 避免「交易所已成交
                    # 但本地仍 UNKNOWN」被误判为持仓漂移。
                    matrix.ingest("recovery", await self.order_recovery.recover(symbol))
                    # V8: 持仓对账(本地 vs 交易所余额)
                    matrix.ingest(
                        "position",
                        await self.reconciler.reconcile_live(self.risk_manager.positions.positions),
                    )
                    # V10.3: lot 总和对账(开仓 lot 总和 vs 持仓量)
                    for _sym, _pos in self.risk_manager.positions.positions.items():
                        lot_diff = self.execution_engine.lot_tracker.reconcile(_sym, _pos.quantity)
                        if lot_diff is not None:
                            matrix.ingest("lot", [{"type": "lot_sum_mismatch", "symbol": _sym, **lot_diff}])
                    # V10.4: 三维交叉对账(Order/Fill/Ledger/Lot 内部一致性)
                    matrix.ingest("cross", await self.cross_reconciler.reconcile(symbol))
                    # V10.7: 交易所真相对账(成交维度)
                    truth_findings = await self.exchange_truth.reconcile(symbol)
                    matrix.ingest("exchange_truth", truth_findings)
                    # V10: 权益对账(本地 vs 交易所)
                    last_price = (
                        self.market_engine.state[symbol].last_price
                        if symbol in self.market_engine.state else 0.0
                    )
                    matrix.ingest(
                        "equity",
                        await self.reconciler.reconcile_account(
                            symbol,
                            self.risk_manager.current_equity,
                            last_price,
                            tolerance_pct=self.settings.equity_reconcile_tolerance_pct,
                        ),
                    )
                    # V11.2 P0-4: 资金级 Circuit Breaker 执行链
                    # (交易所真相 -> 漂移计算 -> FundCircuitBreaker.assess -> 风险态)
                    truth_complete = not any(
                        f.get("type") in ("truth_incomplete", "pagination_exhausted")
                        for f in truth_findings
                    )
                    drift = await self._compute_fund_drift(symbol, last_price, truth_complete)
                    if drift is not None and drift.trusted:
                        decision = self.fund_breaker.assess(
                            equity_drift=drift.equity_drift,
                            position_drift=drift.position_drift,
                            cash_drift=drift.cash_drift,
                        )
                    else:
                        if drift is not None:
                            self.trading_gate.exchange_healthy = False
                            self.logger.warning(
                                "资金漂移不可信(跳过熔断判定)", symbol=symbol, reason=drift.reason,
                            )
                        from at60_risk.fund_circuit_breaker import BreakerDecision

                        decision = BreakerDecision()  # NONE
                    await self._apply_breaker_decision(decision, symbol, drift)
                    # V11.2 P1-2: 资金漂移指标(三向取最大 -> gauge)
                    if drift is not None and drift.trusted:
                        _drifts = [d for d in (
                            drift.equity_drift, drift.position_drift, drift.cash_drift,
                        ) if d is not None]
                        if _drifts:
                            self.metrics.gauge("reconcile_drift_pct", max(_drifts))

                await self._apply_verdict(matrix.verdict())
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("对账循环异常")
            await asyncio.sleep(self.settings.reconcile_interval_seconds)

    async def _apply_verdict(self, verdict) -> None:
        """按对账矩阵判定统一处置: PASS 无动作 / DEGRADED 暂停 / RECOVERY_REQUIRED 暂停自愈 /
        KILLED 急停冻结。所有差异统一在此落日志, 不再由各对账器分别 arm kill。"""
        from at50_execution.observability import record_reconcile_verdict
        from at50_execution.reconciliation_matrix import Severity
        from at60_risk.system_lifecycle import apply_reconcile_verdict

        for f in verdict.findings:
            if f.severity is Severity.PASS:
                self.logger.warning("对账可观测性信号", reconciler=f.reconciler, **f.data)
                continue
            self.logger.error(
                "对账差异", reconciler=f.reconciler, severity=f.severity.value, **f.data,
            )

        # V11.2 P1-2: 对账差异类型计数(api_error / truth_incomplete / pagination_exhausted)
        for f in verdict.findings:
            if f.type in ("api_error", "truth_incomplete", "pagination_exhausted"):
                self.metrics.incr(f.type)

        # V11.2 P0-4: 对账判定反馈到统一闸门(消除默认健康假设)。
        # reconciled = 本周期无 actionable 差异; exchange_healthy = 无 api_error / 真相不完整。
        self.trading_gate.reconciled = verdict.severity is Severity.PASS
        self.trading_gate.exchange_healthy = not any(
            f.type in ("api_error", "truth_incomplete", "pagination_exhausted")
            for f in verdict.findings
        )

        # V11.2 P1-1: 对账判定驱动生命周期迁移(DEGRADED/RECOVERY/SAFE_MODE, PASS 自动恢复交易)。
        reason = verdict.reasons[0] if verdict.reasons else "对账矩阵判定"
        # V11.5 P1-1: 记录最近对账时间 + 最近对账错误(供 runtime health 快照)。
        from at10_web import system_state

        system_state.last_reconcile_at = time.time()
        if verdict.severity is not Severity.PASS:
            system_state.last_error = {
                "ts": time.time(), "source": "对账", "message": reason,
            }
        apply_reconcile_verdict(self.lifecycle, verdict.severity.value, reason=reason)
        record_reconcile_verdict(self.metrics, verdict.severity.value)

        if verdict.severity is Severity.PASS:
            return
        if verdict.severity is Severity.KILLED:
            self.logger.error("对账矩阵判定 KILLED(急停冻结)", reasons=verdict.reasons)
            self.risk_manager.kill_switch.arm(f"对账矩阵 KILLED: {verdict.reasons[0]}")
            await self.risk_manager.kill_switch.persist()
        elif verdict.severity is Severity.RECOVERY_REQUIRED:
            self.logger.warning(
                "对账矩阵判定 RECOVERY_REQUIRED(暂停等待自愈)", reasons=verdict.reasons,
            )
            self.risk_manager.pause(f"对账需恢复: {verdict.reasons[0]}")
        else:  # DEGRADED
            self.logger.warning("对账矩阵判定 DEGRADED(降级暂停)", reasons=verdict.reasons)
            self.risk_manager.pause(f"对账降级: {verdict.reasons[0]}")

    # ---------- V11.2 P0-4: 资金级 Circuit Breaker 执行链 ----------

    async def _compute_fund_drift(self, symbol: str, last_price: float, truth_complete: bool):
        """计算本地 vs 交易所三向资金漂移(equity/position/cash)。

        - 纸面 / 无 REST: 返回 None(无交易所真相, 不做漂移)。
        - get_account 失败: 返回 None(交 reconcile_account 的 api_error 兜底降级)。
        - 否则返回 DriftResult(trusted 或不可信)。
        """
        from at50_execution.drift import compute_drift
        from at50_execution.reconciliation import _split_asset

        rest = self.market_engine.rest
        if self.execution_engine.is_paper or rest is None:
            return None

        try:
            account = await rest.get_account()
        except Exception as e:
            self.logger.warning("资金漂移获取交易所账户失败", symbol=symbol, error=str(e))
            return None

        base, quote = _split_asset(symbol)
        exchange_cash = 0.0
        exchange_position = 0.0
        for bal in account.get("balances", []):
            asset = str(bal.get("asset", ""))
            free = float(bal.get("free", 0) or 0)
            locked = float(bal.get("locked", 0) or 0)
            if asset == quote:
                exchange_cash += free + locked
            elif asset == base:
                exchange_position += free + locked
        exchange_equity = exchange_cash + exchange_position * last_price

        local_equity = self.risk_manager.current_equity
        pos = self.risk_manager.positions.positions.get(symbol)
        local_position = pos.quantity if pos else 0.0
        # 实盘无独立现金账: 由权益恒等式反推 local_cash = equity - position*price
        local_cash = local_equity - local_position * last_price

        return compute_drift(
            local_equity=local_equity,
            exchange_equity=exchange_equity,
            local_position=local_position,
            exchange_position=exchange_position,
            local_cash=local_cash,
            exchange_cash=exchange_cash,
            truth_complete=truth_complete,
            symbol=symbol,
        )

    async def _apply_breaker_decision(self, decision, symbol: str, drift) -> None:
        """BreakerDecision -> 风险态(单一处置点)+ 审计落库。

        REDUCE_ONLY -> 仅减仓; PAUSE -> 暂停; KILL -> 急停持久冻结。
        仅 action != NONE 才处置; 处置动作反馈到统一闸门 last_breaker_action。
        """
        from at50_execution.observability import record_breaker_action
        from at60_risk.fund_circuit_breaker import BreakerAction

        self.trading_gate.last_breaker_action = decision.action

        if not decision.actionable:
            return

        # V11.2 P1-2: 资金熔断动作指标(breaker_reduce_only / breaker_pause / breaker_kill)
        record_breaker_action(self.metrics, decision.action.value)

        if decision.action is BreakerAction.REDUCE_ONLY:
            self.risk_manager.reduce_only(f"资金漂移: {decision.reason}")
        elif decision.action is BreakerAction.PAUSE:
            self.risk_manager.pause(f"资金漂移: {decision.reason}")
        elif decision.action is BreakerAction.KILL:
            self.risk_manager.kill_switch.arm(f"资金漂移: {decision.reason}")
            await self.risk_manager.kill_switch.persist()
            # V11.2 P1-1: 资金级异常 -> SAFE_MODE(冻结, 需人工恢复)
            self.lifecycle.enter_safe_mode(f"资金熔断: {decision.reason}")

        await self._record_breaker_decision(decision, symbol, drift)

    async def _record_breaker_decision(self, decision, symbol: str, drift) -> None:
        """资金熔断决策审计落库(RiskEvent, detail 为 JSON, 可追溯 timestamp/漂移/动作/原因/状态)。"""
        try:
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import RiskEvent

            payload = {
                "source": "fund_breaker",
                "ts": int(time.time()),
                "action": decision.action.value,
                "reason": decision.reason,
                "equity_drift": drift.equity_drift if drift else None,
                "position_drift": drift.position_drift if drift else None,
                "cash_drift": drift.cash_drift if drift else None,
                "lifecycle_state": self.lifecycle.current,
                "risk_state": self.risk_manager.state_machine.state.value,
            }
            async with AsyncSessionLocal() as session:
                session.add(RiskEvent(
                    event_type="fund_breaker",
                    symbol=symbol,
                    detail=json.dumps(payload, ensure_ascii=False),
                    equity=self.risk_manager.current_equity,
                ))
                await session.commit()
        except Exception:
            self.logger.exception("资金熔断决策审计落库失败")

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
        # V11.5 P0-2: 停机后禁止核心仓加仓(ADD=BUY)
        # getattr 兜底: 允许 object.__new__ 构造的最小假系统(无该属性)按「运行中」处理
        if getattr(self, "_shutting_down", False):
            return
        from at55_portfolio.core_manager import CoreAction

        action = decision["action"]
        if action.value not in ("ADD", "REDUCE"):
            return
        # V11.2 P0-2: 统一交易闸门(单一权威); 加仓走 open, 减仓走 reduce
        if action == CoreAction.ADD:
            gate_ok, gate_reason = self.trading_gate.can_open_position()
            if not gate_ok:
                self.logger.info("核心仓加仓被闸门拦截", action=action.value, reason=gate_reason)
                return
            # V12 §16: SOL 总敞口硬上限(核心仓加仓也受 70% 上限约束, 超限禁买)。
            # getattr 兜底: 允许 object.__new__ 构造的最小假系统(无 market_engine)按
            # 「不拦截」处理(与 _shutting_down 兜底语义一致)。
            market_engine = getattr(self, "market_engine", None)
            if market_engine is not None:
                last_prices = {s: st.last_price for s, st in market_engine.state.items()}
                add_qty = decision.get("add_qty") or 0.0
                sol_value = self.risk_manager.positions.total_position_quote(last_prices)
                equity = self.risk_manager.equity(last_prices)
                exposure_cap = equity * self.settings.risk_max_sol_exposure
                if add_qty > 0 and sol_value + add_qty * price > exposure_cap:
                    self.logger.warning(
                        "核心仓加仓被 SOL 敞口硬上限拦截",
                        symbol=symbol, sol_value=round(sol_value, 2),
                        cap=round(exposure_cap, 2),
                        exposure_pct=f"{self.settings.risk_max_sol_exposure:.0%}",
                    )
                    return
        if action == CoreAction.REDUCE:
            gate_ok, gate_reason = self.trading_gate.can_reduce_position()
            if not gate_ok:
                self.logger.info("核心仓减仓被闸门拦截", action=action.value, reason=gate_reason)
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
        """V9.0 + V12 §37: 每日复盘报告(运行状态 + 账户/持仓 + HODL 对标 + 交易活动)"""
        while self._running:
            try:
                symbol = self.settings.symbol_list[0]
                last_prices = {
                    s: st.last_price for s, st in self.market_engine.state.items()
                }
                price = last_prices.get(symbol, 0.0)
                equity = self.risk_manager.equity(last_prices)
                assessment = self.regime_engine.get(symbol) if self.regime_engine else None
                regime = assessment.regime if assessment else ""
                metrics = await self._v12_report_metrics(symbol, equity, price)
                await self.daily_report.generate(
                    symbol, regime=regime, equity=equity, health=self._runtime_health(), metrics=metrics
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("每日复盘循环异常")
            await asyncio.sleep(86400)

    async def _v12_report_metrics(self, symbol: str, equity: float, price: float) -> dict:
        """V12 §37: 组装账户/持仓/HODL 对标指标, 供每日复盘报告使用。

        纯本地组装(仓位/权益来自 RiskManager, 基准来自 HodlBenchmark), 不查交易所;
        任一环节异常仅降级为 0/None, 不阻断报告生成。
        """
        rm = self.risk_manager
        pos = rm.positions.get_or_none(symbol) if rm else None
        sol_qty = pos.quantity if pos else 0.0
        sol_value = sol_qty * price
        usdt = equity - sol_value
        exposure = sol_value / equity if equity > 0 else 0.0
        unrealized = (price - pos.avg_price) * sol_qty if pos else 0.0
        realized = (
            sum(p.realized_pnl for p in rm.positions.positions.values()) if rm else 0.0
        )
        drawdown = 0.0
        if rm:
            try:
                drawdown = float(rm.drawdown.status().get("drawdown", 0.0))
            except Exception:
                drawdown = 0.0
        benchmark = None
        if getattr(self, "hodl_benchmark", None) is not None:
            try:
                benchmark = await self.hodl_benchmark.evaluate(equity, price)
            except Exception:
                benchmark = None
        return {
            "sol_qty": sol_qty,
            "usdt_cash": usdt,
            "price": price,
            "sol_exposure_pct": exposure,
            "unrealized_pnl": unrealized,
            "realized_pnl": realized,
            "drawdown_pct": drawdown,
            "benchmark": benchmark.to_dict() if benchmark else None,
        }

    def _runtime_health(self) -> dict:
        """V11.4 P1-4 + V12 §37: 汇总运行状态快照(生命周期/风险态/急停/熔断/告警 + 交易门/对账)。"""
        rm = self.risk_manager
        lc = self.lifecycle
        gate = getattr(self, "trading_gate", None)
        return {
            "lifecycle": lc.current if lc else "未初始化",
            "risk_state": rm.state_machine.current if rm else "未初始化",
            "risk_reason": (rm.state_machine.reason if rm else "") or "",
            "kill_switch_armed": bool(rm.kill_switch.is_armed) if rm else False,
            "kill_switch_reason": rm.kill_switch.reason if rm else "",
            "breaker_open": bool(rm.breaker.is_open) if rm else False,
            "breaker_reason": rm.breaker.reason if rm else "",
            "alerts": len(self._active_alerts),
            # V12 §37: 交易门(六维快照) + 对账健康
            "trading_gate": gate.snapshot() if gate else {},
        }

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


if __name__ == "__main__":
    asyncio.run(run(AdaptiveTradingSystem))
