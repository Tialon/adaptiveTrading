"""装配(wiring): 把 AdaptiveTradingSystem 的各引擎按依赖顺序装配起来。

`wire_system(system)` 对应原 `AdaptiveTradingSystem.initialize()` 的全部装配逻辑:
日志 / 配置审计 / 建库 / 主网守卫 / 风控 / 执行 / 分析 / 行情 / 对账 / Web 状态注册 /
生命周期推进。它只做装配(import + 赋值 + 少量副作用调用), 不改交易语义; 交易语义
方法仍在 run.py 的 `AdaptiveTradingSystem` 类内(不微服务化)。

V11.6 P2 从 run.py 抽出。此模块与 runtime.py 同为「编排粘合」, 依赖真实引擎 + 网络,
单测难覆盖, 故在 pyproject.toml 的 coverage 中与 run.py 同理由 exclude(见 omit)。
"""

from __future__ import annotations

import time

from at01_common.database import SCHEMA_VERSION, init_db
from at01_common.logger import setup_logging


async def wire_system(system) -> None:
    """装配各引擎"""
    setup_logging()
    system.logger.info(
        "初始化",
        app=system.settings.app_name,
        version=system.settings.app_version,
        symbols=system.settings.symbol_list,
        symbol=system.settings.symbol_list[0] if system.settings.symbol_list else "",
        paper_trading=system.settings.paper_trading,
        testnet=system.settings.binance_testnet,
        git_sha=system.settings.git_sha,
        image_tag=system.settings.image_tag,
        schema_version=SCHEMA_VERSION,
    )

    # V12.4: 写接口鉴权被显式关闭时, 启动就打醒目告警 —— 避免「关了忘了」。
    if system.settings.admin_auth_disabled:
        system.logger.warning(
            "⚠️ 写接口鉴权已关闭(WEB_ADMIN_AUTH=off): 局域网内任何设备无需凭据即可"
            "改配置 / 恢复急停 / 停机。仅建议在个人内网使用; 恢复请在配置里设 WEB_ADMIN_AUTH=on。"
        )

    # V11.2 P1-4: 生产配置审计(实盘需 key、标的非空、三桶比例和 == 1.0), 未通过即拒绝启动
    config_problems = system.settings.validate()
    if config_problems:
        system.logger.error("生产配置审计未通过", problems=config_problems)
        raise RuntimeError("配置校验失败: " + "; ".join(config_problems))

    await init_db()

    # 延迟导入(确保 sys.path 已注入)
    from at20_analytics.engine import AnalyticsEngine
    from at20_analytics.regime import MarketRegimeEngine
    from at20_analytics.alpha import AlphaEngine
    from at60_execution.execution_executor import ExecutionEngine
    from at10_market.market_engine import MarketDataEngine
    from at50_risk.risk_manager import RiskManager
    from at50_risk.risk_portfolio import PortfolioEngine
    from at50_risk.risk_buckets import BucketPositionManager
    from at50_risk.risk_allocation import PortfolioAllocator
    from at50_risk.risk_tiered import TieredDrawdownManager
    from at50_risk.risk_sizing import PositionSizer
    from at30_strategy.strategy_engine import StrategyEngine
    from at30_strategy.strategy_signal_tracker import SignalResultTracker
    from at90_web import system_state

    # 主网启动守卫(默认禁主网: BINANCE_TESTNET=false 需显式 LIVE_TRADING_CONFIRM=true)
    block_reason = system.settings.mainnet_blocked_reason()
    if block_reason:
        system.logger.error("拒绝主网启动", reason=block_reason)
        raise RuntimeError(block_reason)

    # V11.8 §21: 主网就绪自检(BINANCE_TESTNET=false 时强制; 任一不满足 → BLOCKED 拒绝启动)
    # 仅做本地确定性判定(配置/环境/开关), 不查交易所; 真实 go/no-go 复审见 docs/mainnet-readiness.md。
    # kill_switch_armed 此处传 False: 持久化急停态尚未从 DB 载入(下方 load_from_db), 由启动末尾
    # `kill_switch.is_armed` 单独守卫(armed → 停在 READY 不交易), 两处兜底互不重复。
    if not system.settings.binance_testnet:
        from at01_common.mainnet_readiness import (
            format_readiness_report,
            mainnet_readiness_check,
        )
        from at01_common.testnet_gate import git_sha as _git_sha

        readiness = mainnet_readiness_check(
            binance_testnet=system.settings.binance_testnet,
            paper_trading=system.settings.paper_trading,
            live_trading_confirm=system.settings.live_trading_confirm,
            api_scope_confirmed=system.settings.mainnet_api_scope_confirmed,
            symbol=",".join(system.settings.symbol_list),
            config_problems=config_problems,
            kill_switch_armed=False,
            git_sha=_git_sha() or system.settings.git_sha,
            base_url=system.settings.binance_rest_url,
        )
        print(format_readiness_report(readiness))
        if not readiness["allowed"]:
            system.logger.error(
                "主网就绪自检未通过(BLOCKED)",
                reasons=readiness["blocked_reasons"],
            )
            raise RuntimeError(
                "主网就绪自检未通过: " + "; ".join(readiness["blocked_reasons"])
            )

    # V11.7 P1-4: 测试网真实执行闸门(非纸面才需要; 条件不满足 → BLOCKED, 拒绝启动)
    from at01_common.testnet_gate import (
        format_preflight_report,
        git_sha,
        testnet_preflight,
    )

    preflight = testnet_preflight(
        binance_testnet=system.settings.binance_testnet,
        paper_trading=system.settings.paper_trading,
        live_trading=system.settings.live_trading_confirm.strip().lower() == "true",
        run_testnet_trading=system.settings.run_testnet_trading,
        credentials_present=bool(
            system.settings.binance_testnet_api_key
            and system.settings.binance_testnet_api_secret
        ),
        git_sha=git_sha() or system.settings.git_sha,
        symbol=",".join(system.settings.symbol_list),
    )
    system.logger.info("测试网前置检查", report=preflight["report"])
    if not preflight["allowed"]:
        system.logger.error(
            "测试网真实执行前置检查未通过(BLOCKED)",
            reasons=preflight["blocked_reasons"],
        )
        raise RuntimeError(
            "测试网真实执行前置检查未通过: " + "; ".join(preflight["blocked_reasons"])
        )
    print(format_preflight_report(preflight))

    # V12.6 P0: 实盘权益基线播种 —— **必须在任何风控/策略组件构造之前**。
    #
    # 风控模型的一切(equity / max_position_quote / max_sol_exposure_quote /
    # DrawdownController.peak_equity / PortfolioAllocator / 各策略 single_quote 回落)
    # 都建立在 `risk_initial_equity` 之上, 而它出厂默认 100000.0。
    # 此前唯一会覆盖它的逻辑(`mainnet_takeover`)带 `not binance_testnet` 条件, **只跑主网** ——
    # 于是 live_testnet 下本地权益恒为 100000, 与真实账户相差约 100%,
    # 触发 `equity_drift`(单发即 KILLED) → 急停 → SAFE_MODE → 无成交 → 漂移永不收敛 → 永久锁死。
    #
    # 只读交易所账户, 不产生任何订单; 拿不到真实权益即拒绝启动(fail-closed) ——
    # 按虚构的 100000 权益算仓位, 比不启动危险得多。
    if not system.settings.paper_trading:
        from at01_common.live_equity import seed_live_equity_baseline

        seed = await seed_live_equity_baseline(system.settings)
        if not seed["ok"]:
            system.logger.error("实盘权益基线播种失败(BLOCKED)", **seed)
            raise RuntimeError(
                f"实盘权益基线播种失败: {seed['error']}。"
                "无法建立与交易所同源的风控基线, 拒绝启动。"
            )
        system.logger.info(
            "实盘权益基线已按交易所账户播种",
            symbol=seed["symbol"],
            original=seed["original"],
            seeded=seed["seeded"],
            cash=seed["cash"],
            position=seed["position"],
            price=seed["price"],
        )

    # 风控
    system.risk_manager = RiskManager()
    await system.risk_manager.positions.load_from_db()
    # V10: 恢复急停状态(重启后仍保持冻结, 不自动复位)
    await system.risk_manager.kill_switch.load_from_db()

    # V11.1 P1-3: 顶层生命周期状态机(INIT -> WARMING_UP, 其余态随初始化推进)
    from at50_risk.system_lifecycle import SystemLifecycle
    from at50_risk.fund_circuit_breaker import FundCircuitBreaker
    from at60_execution.observability import (
        MetricsStore,
    )

    system.lifecycle = SystemLifecycle()
    system.lifecycle.warm_up()
    system.fund_breaker = FundCircuitBreaker()
    system.metrics = MetricsStore()
    # V11.2 P0-2: 统一交易闸门(单一权威, 组合六维)
    from at50_risk.trading_gate import TradingGate

    system.trading_gate = TradingGate(system.risk_manager, system.lifecycle, system.fund_breaker)

    # V3.0: Portfolio Engine(成本管理) —— 必须先于执行引擎创建(执行引擎据此记账)
    system.portfolio_engine = PortfolioEngine(system.risk_manager.positions)

    # 执行(依赖风控/组合引擎与 REST)
    system.execution_engine = ExecutionEngine(
        risk_manager=system.risk_manager,
        on_fill=system._on_fill,
        portfolio=system.portfolio_engine,  # V3.0: 成本管理
    )
    # V3.0: 执行的信号注册到结果跟踪器
    system.execution_engine.on_signal_registered = system._register_tracked_signal

    # V10.3: 恢复开仓 lot(FIFO 批次追踪, 崩溃后不丢批次)
    await system.execution_engine.lot_tracker.load_from_db()

    # V8: 交易状态机恢复 + 与持仓对账(有持仓但状态丢失 -> HOLDING)
    await system.execution_engine.trade_sm.load_from_db()
    held = {s for s, p in system.risk_manager.positions.positions.items() if p.quantity > 0}
    system.execution_engine.trade_sm.reconcile_with_positions(held)

    # V8: 纸面现金恢复(重启后纸面资金不重置)
    if system.execution_engine.is_paper:
        await system.execution_engine.paper.load_cash_from_db()

    # V3.0: Alpha Engine(综合评分)
    system.alpha_engine = AlphaEngine()
    # V3.0: 信号结果跟踪
    system.signal_tracker = SignalResultTracker()
    await system.signal_tracker.load_open_from_db()

    # V4.0: 双仓/分配/定仓/分级回撤
    system.bucket_manager = BucketPositionManager(system.risk_manager.positions)
    await system.bucket_manager.load_from_db()
    system.allocator = PortfolioAllocator(initial_equity=system.settings.risk_initial_equity)
    system.tiered_dd = TieredDrawdownManager(hard_breaker=system.risk_manager.breaker)
    system.sizer = PositionSizer()

    # V9.0: 组合编排层(核心/交易/现金三桶) + 记忆层(日志/版本/复盘)
    from at40_portfolio.portfolio_manager import PortfolioManager
    from at40_portfolio.core_manager import CorePositionManager
    from at70_journal.trading_journal import TradingJournal
    from at70_journal.daily_report import DailyReport
    from at70_journal.hodl_benchmark import HodlBenchmark
    from at30_strategy.strategy_version import StrategyVersionManager

    system.portfolio_manager = PortfolioManager(system.risk_manager.positions, system.bucket_manager)
    system.core_manager = CorePositionManager(system.bucket_manager)
    system.trading_journal = TradingJournal()
    system.daily_report = DailyReport()
    system.hodl_benchmark = HodlBenchmark(symbol=system.settings.symbol_list[0])
    system.strategy_version = StrategyVersionManager()
    # 成交闭环 -> 日志
    system.execution_engine.on_trade_record = system.trading_journal.record

    # V9.0 M3.4: 情绪因子(Funding+OI, 默认关闭; 不碰现货主链路)
    if system.settings.sentiment_enabled:
        from at10_market.market_futures_client import BinanceFuturesClient
        from at20_analytics.sentiment import SentimentAnalyzer

        futures_client = BinanceFuturesClient()
        await futures_client.connect()
        system.sentiment_analyzer = SentimentAnalyzer(client=futures_client)
        system.logger.info("情绪因子已启用(合约 Funding+OI)")

    # 策略
    system.strategy_engine = StrategyEngine(symbols=system.settings.symbol_list, on_signal=system._on_signal)
    system.strategy_engine.position_provider = system._position_provider
    system.strategy_engine.decision_context = system._decision_context  # V4.0
    system.strategy_engine.setup()

    # 分析
    system.analytics_engine = AnalyticsEngine(
        symbols=system.settings.symbol_list,
        on_analytics=system._on_analytics,
    )

    # 行情
    system.market_engine = MarketDataEngine(
        symbols=system.settings.symbol_list,
        on_trade=system._on_trade,
    )
    await system.market_engine.start()

    # V2.0: Market Regime Engine
    system.regime_engine = MarketRegimeEngine(
        watch_interval=system.settings.regime_watch_interval
    )

    # 注入 REST 客户端供实盘执行
    system.execution_engine.rest = system.market_engine.rest

    # V8: 持仓对账器(仅实盘接 REST; 纸面做现金自检)
    from at60_execution.reconciliation import PositionReconciler

    system.reconciler = PositionReconciler(
        rest_client=None if system.execution_engine.is_paper else system.market_engine.rest,
    )

    # V10.4: 三维交叉对账(Order/Fill/Ledger/Lot 一致性, 纯 DB 读, 无需 REST)
    from at60_execution.cross_reconciler import CrossReconciler

    system.cross_reconciler = CrossReconciler()

    # V10.7: 订单恢复引擎(仅实盘; UNKNOWN/RECOVERY_REQUIRED 周期收敛 + 账务重建)
    from at60_execution.order_recovery import OrderRecoveryEngine

    system.order_recovery = OrderRecoveryEngine(
        rest_client=None if system.execution_engine.is_paper else system.market_engine.rest,
        execution_engine=system.execution_engine,
        risk_manager=system.risk_manager,
    )

    # V10.7: 交易所真相对账(订单/成交维度: 本地 filled_quantity vs 交易所 myTrades)
    from at60_execution.exchange_truth_reconciler import ExchangeTruthReconciler

    system.exchange_truth = ExchangeTruthReconciler(
        rest_client=None if system.execution_engine.is_paper else system.market_engine.rest,
    )

    # V12 §10-11: 主网首次只读接管(仅主网实盘 + 启用 + 尚无基线): 账户快照 + 对账 + HODL 基线。
    # 首次接管是一次性的 go/no-go 门(意外挂单/持仓漂移 -> 急停冻结); 重启恢复由下方
    # startup_reconciler 负责, 二者不重复。基线冻结见 HodlBenchmark.record_baseline。
    if (
        not system.settings.paper_trading
        and not system.settings.binance_testnet
        and system.settings.mainnet_takeover_enabled
        and not await system.hodl_benchmark.has_baseline()
    ):
        from at60_execution.mainnet_takeover import MainnetTakeover

        symbol = system.settings.symbol_list[0]
        local_pos = system.risk_manager.positions.get_or_none(symbol)
        local_sol_qty = local_pos.quantity if local_pos else 0.0
        system.mainnet_takeover = MainnetTakeover(
            rest_client=system.market_engine.rest,
            symbol=symbol,
            hodl_benchmark=system.hodl_benchmark,
        )
        result = await system.mainnet_takeover.takeover(local_sol_qty=local_sol_qty)
        system.logger.info(
            "主网首次接管完成",
            allowed=result["allowed"],
            baseline_recorded=result["baseline_recorded"],
            reconciliation=result["reconciliation"],
            snapshot=result["snapshot"],
        )
        if not result["allowed"]:
            system.risk_manager.kill_switch.arm(
                "主网首次接管未通过: " + "; ".join(result["blocked_reasons"])
            )
            system.logger.error(
                "主网首次接管未通过, 已冻结交易",
                blocked_reasons=result["blocked_reasons"],
            )
        await system.risk_manager.kill_switch.persist()

    # V10: 启动对账(仅实盘 + 启用): 崩溃窗口恢复 + 未解决差异 -> 急停冻结
    if not system.execution_engine.is_paper and system.settings.startup_reconcile_enabled:
        from at60_execution.startup_reconciler import StartupReconciler

        system.startup_reconciler = StartupReconciler(
            rest_client=system.market_engine.rest,
            execution_engine=system.execution_engine,
            risk_manager=system.risk_manager,
        )
        for symbol in system.settings.symbol_list:
            diffs = await system.startup_reconciler.reconcile(symbol)
            if diffs:
                summary = "; ".join(
                    f"{d.get('type')}:{d.get('exchange_order_id') or d.get('client_order_id') or d.get('symbol', '*')}"
                    for d in diffs
                )
                system.risk_manager.kill_switch.arm(f"启动对账未通过: {summary}")
                system.logger.error("启动对账未通过, 已冻结交易", symbol=symbol, diffs=diffs)
        await system.risk_manager.kill_switch.persist()

    # V8: 行情数据异常回调 -> 暂停交易
    system.market_engine.on_data_anomaly = system._on_data_anomaly

    # 注册 Web 状态
    system_state.market_engine = system.market_engine
    system_state.analytics_engine = system.analytics_engine
    system_state.strategy_engine = system.strategy_engine
    system_state.risk_manager = system.risk_manager
    system_state.execution_engine = system.execution_engine
    system_state.regime_engine = system.regime_engine
    system_state.trading_gate = system.trading_gate
    system_state.metrics = system.metrics
    system_state.lifecycle = system.lifecycle
    # V11.5 P1-1: 后台任务监督器(供 runtime health 读 active tasks / task failures)
    system_state.supervisor = system.supervisor
    system_state.running = True
    system_state.started_at = time.time()

    # V9.0: 启动基线版本快照(每日一份, 同版本去重)
    from datetime import datetime, timezone

    baseline = f"{system.settings.app_version}-{datetime.now(tz=timezone.utc).strftime('%Y%m%d')}"
    await system.strategy_version.snapshot(baseline, note="startup baseline")

    # V11.2 P1-1: 生命周期推进到就绪/交易态; 启动对账未通过(急停)则停在 READY 不交易
    system.lifecycle.sync()
    system.lifecycle.self_check()
    system.lifecycle.ready()
    if not system.risk_manager.kill_switch.is_armed:
        system.lifecycle.start_trading()
    else:
        system.logger.warning("启动对账未通过, 生命周期停留在 READY(不交易)")

    system.logger.info("系统初始化完成")
