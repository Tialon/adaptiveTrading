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
from typing import Any

from at01_common.bootstrap import inject_sys_path

# 保证各模块包可导入(atXX 号码分层目录), 必须在任何 atXX 包 import 之前执行
inject_sys_path()

from at01_common.database import close_db  # noqa: E402
from at01_common.settings import get_settings  # noqa: E402
from at01_common.logger import get_logger  # noqa: E402
from at01_common.operator_events import (  # noqa: E402
    KIND_DEGRADE,
    KIND_ERROR,
    KIND_FILL,
    KIND_KILL,
    KIND_ORDER_SUBMIT,
    KIND_RECONCILE,
    KIND_RECOVERY,
    KIND_RISK_BLOCK,
    KIND_RISK_PASS,
    KIND_SHUTDOWN,
    KIND_SIGNAL,
    KIND_STARTUP,
    KIND_TRADE_DONE,
    operator_log,
)
from at01_common.operator_narrative import (  # noqa: E402
    KILL_ORIGIN_AUTO_EQUITY,
    KILL_ORIGIN_AUTO_RECONCILE,
    KILL_ORIGIN_AUTO_TASK,
)
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
        # V13: 增加有界自动重启 —— 关键任务崩一次不再必须由人重启进程。
        #      并区分「任务缺席」与「已冻结」: 重启窗口里任务确实不在跑,
        #      那段时间必须禁开仓(否则等于在监控缺失时继续交易)。
        self.supervisor = RuntimeSupervisor(
            on_critical_failure=self._handle_critical_task_failure,
            restart_factory=self._restart_critical_task,
            on_critical_down=self._on_critical_task_down,
            on_critical_recovered=self._on_critical_task_recovered,
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
        self.guard_override = None  # V12.6 P2: 启动守卫解锁状态(在 wiring 中解析)
        self.mode_resolution = None  # V12.7: 运行模式解析结果(在 wiring 中解析)
        self.portfolio_manager = None  # V9.0: 组合编排薄层
        self.core_manager = None  # V9.0: 核心仓低频管理
        self.trading_journal = None  # V9.0: 成交日志
        self.daily_report = None  # V9.0: 每日复盘
        self.hodl_benchmark = None  # V12 §24: HODL 基准(接管基线 + 每日对标)
        self.strategy_version = None  # V9.0: 策略版本快照
        self.sentiment_analyzer = None  # V9.0 M3.4: 情绪因子(默认关闭)
        self.lifecycle = None  # V11.1 P1-3: 顶层生命周期状态机
        self.auto_recovery = None  # V13: 自动恢复编排(仅可自愈来源)
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
        from at90_web.web_app import start_server

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
        # V13: 操作员事件流首条 —— 让「今天发生了什么」从启动那一刻就有记录
        operator_log.emit(
            KIND_STARTUP,
            "系统启动",
            level="NORMAL",
            symbol=self.settings.symbol_list[0] if self.settings.symbol_list else "",
            detail={
                "api": f"http://{self.settings.api_host}:{self.settings.api_port}",
                "tasks": len(tasks),
                "version": self.settings.app_version,
                "git_sha": self.settings.git_sha,
            },
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
        # V13: 停机前把在途的操作员事件落库(close_db 之后写不进去了)
        operator_log.emit(KIND_SHUTDOWN, "系统停机", level="ACTION_REQUIRED")
        await operator_log.flush()
        await close_db()
        self.logger.info("系统已停止")

    def _on_critical_task_down(self, name: str) -> None:
        """关键任务缺席(崩溃的那一刻, 无论接下来是重启还是冻结)。

        **立刻关掉开仓许可** —— 重启需要退避等待, 那段时间风险监控/对账并没有在跑,
        闸门若仍以为一切健康, 就会在监控缺失的情况下继续交易。
        """
        gate = getattr(self, "trading_gate", None)
        if gate is not None:
            gate.critical_tasks_healthy = False
        # V11.5 P1-1: 记录最近一次运行时错误(供 runtime health 快照)。
        # V13: 挪到**这里**而不是冻结回调 —— 运行时健康应当反映「有任务崩了」这个事实,
        # 与「接下来是重启还是冻结」无关。
        try:
            from at90_web import system_state

            system_state.last_error = {
                "ts": time.time(), "source": f"critical 任务 {name}",
                "message": "任务异常退出, 已暂停开新仓",
            }
        except Exception:
            pass
        operator_log.emit(
            KIND_ERROR, f"关键后台任务「{name}」已停止, 暂停开新仓",
            level="DEGRADED", detail={"actor": "auto", "task": name},
        )

    def _on_critical_task_recovered(self, name: str) -> None:
        """关键任务回来了。**只有全部关键任务都在跑**才恢复开仓许可。"""
        if not self.supervisor.critical_tasks_healthy:
            return  # 还有别的关键任务缺席, 继续禁开仓
        gate = getattr(self, "trading_gate", None)
        if gate is not None:
            gate.critical_tasks_healthy = True
        operator_log.emit(
            KIND_RECOVERY, f"关键后台任务「{name}」已恢复运行", level="NORMAL",
            detail={"actor": "auto", "task": name},
        )

    def _restart_critical_task(self, name: str) -> Any:
        """V13: 给监督器一个「怎么重建这个任务」的工厂。

        ⚠️ 协程对象是**一次性**的 —— 不能复用崩掉的那个。这里按名字返回一个**新的**协程。

        `#rN` 后缀是重启任务的命名(基线名仍是原任务名), 所以先剥掉后缀再匹配。
        """
        base = name.split("#r", 1)[0]
        builders = {
            "risk-loop": lambda: self._risk_loop(),
            "reconcile-loop": lambda: self._reconcile_loop(),
        }
        make = builders.get(base)
        if make is None:
            self.logger.warning("该 critical 任务不支持自动重启", task=base)
            return None
        operator_log.emit(
            KIND_RECOVERY, f"关键后台任务「{base}」异常退出, 正在自动重启",
            level="NOTICE", detail={"actor": "auto", "task": base},
        )
        return make()

    def _handle_critical_task_failure(self, name: str, exc: BaseException) -> None:
        """V11.5 P0-2: critical 后台任务异常退出 → 进入安全状态(急停 + SAFE_MODE)。

        由 RuntimeSupervisor 在 done 回调里同步调用(不阻塞事件循环), 立即:
        1. **先关闸门**(BUY 安全契约: 关键任务未运行 → 禁开仓);
        2. arm 急停(内存态, 即刻生效);
        3. 生命周期进入 SAFE_MODE;
        4. fire-and-forget 持久化急停(重启后仍保持冻结)。

        **V13 调整了动作顺序**: 关闸门从「最后一步」提到「第一步」, 且每个动作各自独立守护。
        此前三步共用一个 try, 若第 2 步(arm)抛异常, 第 1 步的闸门翻转就**永远不执行** ——
        结果是「关键任务已死但 BUY 仍然放行」, 属于 fail-open。安全动作之间不该有这种
        级联依赖: 任何一个失败, 其余仍须生效。
        """
        self.logger.error("critical 后台任务异常退出, 进入安全状态", task=name, error=repr(exc))

        # 1) 最优先: 关掉开仓许可。这一步绝不能因为别处失败而被跳过。
        try:
            gate = getattr(self, "trading_gate", None)
            if gate is not None:
                gate.critical_tasks_healthy = False
        except Exception:
            self.logger.exception("关闭开仓许可失败", task=name)

        # V11.5 P1-1: 记录最近一次运行时错误(供 runtime health 快照)。
        try:
            from at90_web import system_state

            system_state.last_error = {
                "ts": time.time(), "source": f"critical 任务 {name}", "message": repr(exc),
            }
        except Exception:
            pass
        try:
            if self.risk_manager is not None:
                # V13: 标注来源 —— 关键任务崩溃属「系统可自愈」类, 允许自动恢复介入
                # (与人工急停、资金异常区分开, 后两者永远要人)。见 at50_risk/auto_recovery.py。
                self.risk_manager.kill_switch.arm(
                    f"critical 任务 {name} 异常退出", origin=KILL_ORIGIN_AUTO_TASK
                )
        except Exception:
            self.logger.exception("急停 arm 失败", task=name)
        try:
            if self.lifecycle is not None:
                self.lifecycle.enter_safe_mode(f"critical 任务 {name} 异常退出")
        except Exception:
            self.logger.exception("进入安全模式失败", task=name)
        try:
            if self.risk_manager is not None:
                self._pending_tasks.add(
                    asyncio.create_task(self._persist_critical_kill_switch())
                )
        except Exception:
            self.logger.exception("critical 急停持久化投递失败", task=name)
        # V13: 这是「最该让用户知道」的自动急停之一, 必须落进操作员事件流
        operator_log.emit(
            KIND_ERROR, f"关键后台任务「{name}」异常退出", level="KILLED",
            detail={"task": name, "error": repr(exc)},
        )
        operator_log.emit(
            KIND_KILL, "系统已自动停止交易(关键后台任务异常)",
            level="KILLED", detail={"actor": "auto", "origin": KILL_ORIGIN_AUTO_TASK,
                                    "reason": f"critical 任务 {name} 异常退出"},
        )

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
        from at60_execution.observability import record_execution

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

            # V13: 操作员事件流 —— 从「发现信号」开始, 让用户能读懂这一笔的来龙去脉
            operator_log.emit(
                KIND_SIGNAL,
                f"发现 {sig.side.value} 信号({sig.strategy})",
                symbol=sig.symbol, ref_type="signal", ref_id=str(getattr(sig, "id", "") or ""),
                detail={"side": sig.side.value, "score": getattr(sig, "score", None),
                        "price": getattr(sig, "price", None)},
            )

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
                # 风控拦下信号是**正常的预期结果**(它本来就在干这个), 不是故障 ——
                # 用 NOTICE 而非 DEGRADED, 否则时间线上天天一片降级色。
                operator_log.emit(
                    KIND_RISK_BLOCK, f"风控未通过, 本次 {sig.side.value} 未执行",
                    level="NOTICE", symbol=sig.symbol,
                    detail={"side": sig.side.value, "reason": decision.reason},
                )
                return
            operator_log.emit(
                KIND_RISK_PASS, "风控通过", symbol=sig.symbol,
                detail={"side": sig.side.value, "quantity": decision.quantity},
            )

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
            operator_log.emit(
                KIND_ORDER_SUBMIT, f"{sig.side.value} 订单已提交", symbol=sig.symbol,
                detail={"side": sig.side.value, "quantity": sig.quantity, "price": sig.price},
            )
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
                    # V13: 「成交 → 持仓更新 → 交易完成」是用户最关心的三行
                    operator_log.emit(
                        KIND_FILL, f"{sig.side.value} 已成交 @ {fill_price}",
                        symbol=sig.symbol, ref_type="order",
                        ref_id=str(result.get("client_order_id") or ""),
                        detail={"side": sig.side.value, "fill_qty": fill_qty,
                                "fill_price": fill_price, "status": result.get("status"),
                                "latency_ms": round(latency_ms, 1)},
                    )
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
                    operator_log.emit(
                        KIND_TRADE_DONE, f"{sig.side.value} 交易完成", symbol=sig.symbol,
                        detail={"side": sig.side.value, "fill_qty": fill_qty,
                                "fill_price": fill_price},
                    )
            else:
                # 执行引擎返回 None(闸门/尺寸/资金不足等拒绝) -> 记一次拒绝尝试
                record_execution(
                    self.metrics, status="REJECTED", latency_ms=latency_ms, strategy=sig.strategy,
                )
                operator_log.emit(
                    KIND_ERROR, f"{sig.side.value} 未执行(执行引擎拒绝)",
                    level="NOTICE", symbol=sig.symbol,
                    detail={"side": sig.side.value},
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
            from at90_web import broadcast

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
        from at90_web import system_state
        from at60_execution.observability import evaluate_alerts

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
                # V13: 自动恢复 —— 冻结状态下由系统自己尝试解除(**仅可自愈来源**)。
                # 放在健康位更新之后: 恢复前置条件读的正是刚更新的那几个位。
                if self.auto_recovery is None and self.trading_gate is not None:
                    from at50_risk.auto_recovery import AutoRecoveryCoordinator

                    self.auto_recovery = AutoRecoveryCoordinator(
                        self.risk_manager, self.lifecycle, self.trading_gate
                    )
                if self.auto_recovery is not None:
                    try:
                        outcome = await self.auto_recovery.tick()
                        if outcome.get("action") == "recovered":
                            self.logger.info("自动恢复完成", steps=outcome.get("steps"))
                    except Exception:
                        self.logger.exception("自动恢复周期异常")
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

    def _reconcile_transition(self, severity: str) -> str:
        """对账严重度的**变化方向** —— 决定记哪种事件(见 `_reconcile_loop`)。

        返回:

            "none"       没有变化 → 什么都不记(对账每 5 分钟一轮, 逐轮记会淹没时间线)
            "first"      本次启动后的第一条 → 记「对账完成」基线
            "recovered"  由异常回到 PASS   → 记「系统已自动恢复」
            "degraded"   由 PASS 转异常    → 记「对账发现差异」

        ⚠️ `"first"` 与 `"recovered"` 必须分开: 启动后的第一次对账**没有可恢复的东西**,
        把它记成 RECOVERY 会让「今日系统复盘」虚报「自动恢复 N 次」—— 实测踩到过,
        重启几次就显示自动恢复了 3 次, 而实际上一次都没有。
        """
        previous = getattr(self, "_last_reconcile_severity", None)
        self._last_reconcile_severity = severity
        # V14 实测: 连续失败次数是「能不能自愈」的关键判据。
        # 实测踩到过: 测试网账户里残留着本地账本不认识的持仓 → `position:mismatch`
        # 每一轮都失败, 而页面一直显示「系统正在自动恢复, 无需操作」——
        # 那是一个**永远不会兑现的承诺**。用户点了恢复、几秒后又冻上, 只会觉得按钮坏了。
        if severity == "PASS":
            self._reconcile_fail_streak = 0
        else:
            self._reconcile_fail_streak = getattr(self, "_reconcile_fail_streak", 0) + 1
        try:
            # 状态容器不持有编排器句柄, 所以经 `extra` 把计数传给健康快照
            from at90_web import system_state

            system_state.extra["reconcile_fail_streak"] = self._reconcile_fail_streak
        except Exception:
            pass
        if previous == severity:
            return "none"
        if previous is None:
            return "first"
        return "recovered" if severity == "PASS" else "degraded"

    async def _reconcile_loop(self) -> None:
        """V11.1(P0-5): 周期对账 —— 统一经对账矩阵判定(单一 kill 决策点, 单一对账器不得 kill)"""
        from at60_execution.reconciliation_matrix import ReconciliationMatrix, Severity

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
                        from at50_risk.fund_circuit_breaker import BreakerDecision

                        decision = BreakerDecision()  # NONE
                    await self._apply_breaker_decision(decision, symbol, drift)
                    # V11.2 P1-2: 资金漂移指标(三向取最大 -> gauge)
                    if drift is not None and drift.trusted:
                        _drifts = [d for d in (
                            drift.equity_drift, drift.position_drift, drift.cash_drift,
                        ) if d is not None]
                        if _drifts:
                            self.metrics.gauge("reconcile_drift_pct", max(_drifts))

                verdict = matrix.verdict()
                await self._apply_verdict(verdict)
                # V13: 对账是本系统最频繁的自动复核(默认每 5 分钟一轮 ≈ 288 次/天),
                # 逐轮落库会把「今天发生了什么」淹成一片「对账完成」。只在**严重度变化**时记,
                # 且区分「启动基线」与「真的从异常恢复」—— 后者才是要计数的自动恢复。
                transition = self._reconcile_transition(verdict.severity.value)
                _detail = {"actor": "auto", "severity": verdict.severity.value,
                           "reasons": list(verdict.reasons)[:5]}
                if transition == "first":
                    operator_log.emit(
                        KIND_RECONCILE,
                        "对账完成, 未发现差异" if verdict.severity is Severity.PASS
                        else f"对账发现差异({verdict.severity.value})",
                        level="NORMAL" if verdict.severity is Severity.PASS else "DEGRADED",
                        detail=_detail,
                    )
                elif transition == "recovered":
                    operator_log.emit(
                        KIND_RECOVERY, "对账恢复正常, 系统已自动恢复",
                        level="NORMAL", detail=_detail,
                    )
                elif transition == "degraded":
                    operator_log.emit(
                        KIND_RECONCILE, f"对账发现差异({verdict.severity.value})",
                        level="DEGRADED", detail=_detail,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("对账循环异常")
            await asyncio.sleep(self.settings.reconcile_interval_seconds)

    async def _apply_verdict(self, verdict) -> None:
        """按对账矩阵判定统一处置: PASS 无动作 / DEGRADED 暂停 / RECOVERY_REQUIRED 暂停自愈 /
        KILLED 急停冻结。所有差异统一在此落日志, 不再由各对账器分别 arm kill。"""
        from at60_execution.observability import record_reconcile_verdict
        from at60_execution.reconciliation_matrix import Severity
        from at50_risk.system_lifecycle import apply_reconcile_verdict

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
        from at90_web import system_state

        system_state.last_reconcile_at = time.time()
        if verdict.severity is not Severity.PASS:
            system_state.last_error = {
                "ts": time.time(), "source": "对账", "message": reason,
            }
        apply_reconcile_verdict(self.lifecycle, verdict.severity.value, reason=reason)
        record_reconcile_verdict(self.metrics, verdict.severity.value)

        if verdict.severity is Severity.PASS:
            return
        if verdict.severity is Severity.RECOVERY_REQUIRED:
            operator_log.emit(
                KIND_DEGRADE, "系统降级: 正在自动恢复", level="DEGRADED",
                detail={"reason": reason, "action": "已暂停开新仓, 保留安全离场通道"},
            )
        if verdict.severity is Severity.KILLED:
            self.logger.error("对账矩阵判定 KILLED(急停冻结)", reasons=verdict.reasons)
            # V13: 标为对账类来源 —— 账户与账本对不上 = 金融状态不明, 自动恢复不介入
            self.risk_manager.kill_switch.arm(
                f"对账矩阵 KILLED: {verdict.reasons[0]}", origin=KILL_ORIGIN_AUTO_RECONCILE
            )
            await self.risk_manager.kill_switch.persist()
            operator_log.emit(
                KIND_KILL, "系统已自动停止交易(账户与账本对不上)", level="KILLED",
                symbol=verdict.symbol if hasattr(verdict, "symbol") else "",
                detail={"actor": "auto", "origin": KILL_ORIGIN_AUTO_RECONCILE,
                        "reasons": list(verdict.reasons)},
            )
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
        from at60_execution.drift import compute_drift
        from at60_execution.reconciliation import _split_asset

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
        from at60_execution.observability import record_breaker_action
        from at50_risk.fund_circuit_breaker import BreakerAction

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
            # V13: 资金漂移 = 权益类重大资金异常, 自动恢复不介入
            self.risk_manager.kill_switch.arm(
                f"资金漂移: {decision.reason}", origin=KILL_ORIGIN_AUTO_EQUITY
            )
            await self.risk_manager.kill_switch.persist()
            # V11.2 P1-1: 资金级异常 -> SAFE_MODE(冻结, 需人工恢复)
            self.lifecycle.enter_safe_mode(f"资金熔断: {decision.reason}")
            operator_log.emit(
                KIND_KILL, "系统已自动停止交易(账户资金异常)", level="KILLED",
                detail={"actor": "auto", "origin": KILL_ORIGIN_AUTO_EQUITY,
                        "reason": decision.reason},
            )

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
        from at40_portfolio.core_manager import CoreAction

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

        from at30_strategy.strategy_base import Signal, SignalSide

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
                # V13: 复盘数据**只算一次**(ai_review 构建), 两种读物各取所需:
                #   人 → reports/<日期>.md 的「今日系统复盘」段落;
                #   AI → review/<日期>/ 的 JSON 包 + ai_review.md。
                # 两处各算一遍迟早会打架, 而「今天赚了多少」两份报告对不上,
                # 比数字本身错更让人失去信任。
                self_review = await self._build_self_review(symbol)
                await self.daily_report.generate(
                    symbol, regime=regime, equity=equity,
                    health=self._runtime_health(), metrics=metrics,
                    self_review=self_review,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("每日复盘循环异常")
            await asyncio.sleep(86400)

    async def _build_self_review(self, symbol: str) -> dict | None:
        """构建当日 AI 复盘包并落盘(旁挂产物, 失败只记日志不影响日报)。"""
        try:
            from at70_journal.ai_review import AIReviewBuilder

            if getattr(self, "_ai_review_builder", None) is None:
                self._ai_review_builder = AIReviewBuilder(
                    symbol=symbol,
                    report_root=getattr(self.settings, "ai_review_dir", "review"),
                )
            result = await self._ai_review_builder.write_package()
            return result.get("package")
        except Exception:
            self.logger.exception("AI 复盘包生成失败")
            return None

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
