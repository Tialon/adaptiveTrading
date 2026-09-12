"""
Portfolio Backtest(V7.0 — Real Strategy Pipeline)

V6 遗留的结构性问题修复(本轮):
1. 删除内嵌的 "+3%/-3%/15%" 隐藏交易策略 —— 回测现在驱动真实
   StrategyEngine + DecisionEngine(与实盘同一代码)
2. 滑点/价差执行模型(SlippageModel, 默认 10bps, 可敏感性)
3. 信号 t 收盘产生 -> t+1 开盘成交(NextBarExecutor, 杀 look-ahead)
4. BTC asof 对齐 + 数据龄检查(替代精确 timestamp match)
5. interval 正确分页(bars_per_day)
6. 数量由 PositionSizer 决定(Decision 数量仅为参考)

保留 V6 的正确性基建:
- PortfolioLedger 双仓独立成本 + 对账
- MarketRegimeEngine 共用
- risk_adjustment_factor 真实输入(BTC + 组合回撤)

说明: 策略信号驱动用简化喂给 StrategyEngine 的 analytics
(每根收盘 bar 合成 4 笔 OHLC tick 进 AnalyticsEngine, 与 V2 回测一致),
策略/决策/仓位代码与实盘完全一致。
"""

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin
from at01_common.timeframe import bars_per_day, bars_per_year
from at30_strategy.strategy_group import group_of


@dataclass
class PortfolioBacktestResult:
    """组合回测结果"""

    symbol: str = ""
    bars: int = 0
    rebalances: int = 0
    trade_count: int = 0
    final_equity: float = 0.0
    total_return: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    # V9.0 M2.3: 新增 6 项指标(win_rate/profit_factor/holding/sortino/calmar/attribution)
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_holding_seconds: float = 0.0
    closed_trades: int = 0
    sortino: float = 0.0
    calmar: float = 0.0
    attribution: dict[str, float] = field(default_factory=dict)
    final_core_qty: float = 0.0
    final_trade_qty: float = 0.0
    core_contribution: float = 0.0
    trade_contribution: float = 0.0
    reconciliation: dict[str, Any] = field(default_factory=dict)
    slippage_bps: float = 10.0
    equity_curve: list[float] = field(default_factory=list)
    exposure_curve: list[float] = field(default_factory=list)
    core_curve: list[float] = field(default_factory=list)
    trade_curve: list[float] = field(default_factory=list)
    cash_curve: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "bars": self.bars,
            "rebalances": self.rebalances, "trades": self.trade_count,
            "total_return": round(self.total_return, 4),
            "benchmark_return": round(self.benchmark_return, 4),
            "excess_return": round(self.excess_return, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe": round(self.sharpe, 3),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 3),
            "avg_holding_seconds": round(self.avg_holding_seconds, 0),
            "closed_trades": self.closed_trades,
            "sortino": round(self.sortino, 3),
            "calmar": round(self.calmar, 3),
            "attribution": {k: round(v, 2) for k, v in self.attribution.items()},
            "final_core_qty": round(self.final_core_qty, 4),
            "final_trade_qty": round(self.final_trade_qty, 4),
            "core_pnl": round(self.core_contribution, 2),
            "trade_pnl": round(self.trade_contribution, 2),
            "balanced": self.reconciliation.get("balanced"),
            "slippage_bps": self.slippage_bps,
        }


def compute_closed_trade_metrics(closed_trades: list[dict[str, Any]]) -> dict[str, Any]:
    """V9.0 M2.3: 从闭环成交列表算 win_rate / profit_factor / holding / 归因(纯函数, 便于测试)"""
    n = len(closed_trades)
    if n == 0:
        return {
            "win_rate": 0.0, "profit_factor": 0.0, "avg_holding_seconds": 0.0,
            "closed_trades": 0, "attribution": {},
        }
    wins = sum(1 for t in closed_trades if t["realized_pnl"] > 0)
    gross_profit = sum(t["realized_pnl"] for t in closed_trades if t["realized_pnl"] > 0)
    gross_loss = -sum(t["realized_pnl"] for t in closed_trades if t["realized_pnl"] < 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    attribution: dict[str, float] = {}
    for t in closed_trades:
        key = t["strategy"] or "unknown"
        attribution[key] = attribution.get(key, 0.0) + t["realized_pnl"]
    return {
        "win_rate": wins / n,
        "profit_factor": profit_factor,
        "avg_holding_seconds": sum(t["holding_seconds"] for t in closed_trades) / n,
        "closed_trades": n,
        "attribution": attribution,
    }


def compute_sortino(returns: list[float], bars_per_year_val: float) -> float:
    """V9.0 M2.3: Sortino(下行偏差, 目标 0)"""
    if not returns:
        return 0.0
    mean_r = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    dd_std = math.sqrt(sum(r * r for r in downside) / len(downside))
    if dd_std <= 0:
        return 0.0
    return (mean_r / dd_std) * math.sqrt(bars_per_year_val)


def compute_calmar(total_return: float, max_drawdown: float) -> float:
    """V9.0 M2.3: Calmar(total_return / max_drawdown)"""
    return total_return / max_drawdown if max_drawdown > 0 else 0.0


async def fetch_klines_paged(
    symbol: str,
    interval: str = "1m",
    days: int = 7,
    max_bars: int = 43200,
) -> list[list[Any]]:
    """V7: 按 interval 正确分页(days × bars_per_day)"""
    from at10_market.market_rest_client import BinanceRestClient

    target = min(days * bars_per_day(interval), max_bars)
    client = BinanceRestClient()
    await client.connect()
    try:
        all_klines: list[list[Any]] = []
        end_time: Optional[int] = None
        while len(all_klines) < target:
            kwargs: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": 1000}
            if end_time:
                kwargs["end_time"] = end_time
            page = await client.get_klines(**kwargs)
            if not page:
                break
            all_klines = page + all_klines
            end_time = int(page[0][0]) - 1
            if len(page) < 1000:
                break
        return all_klines[-target:]
    finally:
        await client.disconnect()


class PortfolioBacktester(LoggerMixin):
    """组合回测器(V7: 真实策略管线 + 次bar执行 + 滑点)"""

    def __init__(
        self,
        symbol: str = "SOLUSDT",
        initial_cash: float = 20000.0,
        fee_rate: float = 0.001,
        interval: str = "1m",
        slippage_bps: float = 10.0,
        regime_eval_bars: int = 60,
        rebalance_tolerance: float = 0.05,
        strategy_eval_bars: int = 5,  # 每 5 根 bar 跑一次策略(性能)
    ):
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.interval = interval
        self.slippage_bps = slippage_bps
        self.regime_eval_bars = regime_eval_bars
        self.rebalance_tolerance = rebalance_tolerance
        self.strategy_eval_bars = strategy_eval_bars

    async def run(
        self,
        klines: list[list[Any]],
        btc_klines: Optional[list[list[Any]]] = None,
    ) -> PortfolioBacktestResult:
        """执行组合回测(真实策略管线)"""
        from at10_market.market_models import TradeTick
        from at20_analytics.engine import AnalyticsEngine
        from at20_analytics.regime import MarketRegimeEngine
        from at50_risk.risk_allocation import PortfolioAllocator
        from at50_risk.risk_ledger import PortfolioLedger
        from at50_risk.risk_sizing import PositionSizer
        from at80_backtest.backtest_execution import AsOfJoiner, NextBarExecutor, SlippageModel

        result = PortfolioBacktestResult(
            symbol=self.symbol, bars=len(klines), slippage_bps=self.slippage_bps
        )
        if len(klines) < 100:
            self.logger.warning("K线不足", bars=len(klines))
            return result

        # ---- 确保持久化表存在(回测驱动的 StrategyEngine 会落库 journal/信号) ----
        try:
            from at01_common.database import init_db

            await init_db()
        except Exception:
            self.logger.warning("回测建表失败, 持久化将跳过")

        # ---- 与实盘相同的组件 ----
        analytics = AnalyticsEngine(symbols=[self.symbol])
        regime_engine = MarketRegimeEngine()
        allocator = PortfolioAllocator(
            initial_equity=self.initial_cash,
            rebalance_tolerance=self.rebalance_tolerance,
        )
        sizer = PositionSizer()
        ledger = PortfolioLedger()
        ledger.init_cash(self.initial_cash)

        from at30_strategy.strategy_engine import StrategyEngine

        strategy_engine = StrategyEngine(symbols=[self.symbol])
        # position provider: 只暴露交易仓(与实盘一致)
        strategy_engine.position_provider = lambda sym: (
            (ledger.qty(sym, "trade"), ledger.avg_cost(sym, "trade"), 0.0)
            if ledger.qty(sym, "trade") > 0 else None
        )
        strategy_engine.setup()

        # V9.0 M3.2: 注入 regime 条件滑点覆盖表(settings 解析)
        from at01_common.settings import get_settings

        slippage = SlippageModel(
            self.slippage_bps, get_settings().slippage_regime_bps_map
        )
        next_bar = NextBarExecutor()
        btc = AsOfJoiner(btc_klines) if btc_klines else None

        peak_equity = self.initial_cash
        portfolio_drawdown = 0.0
        rebalances = 0
        trades = 0
        benchmark_entry_price: Optional[float] = None
        bar_tick_id = 0

        equity_curve: list[float] = []
        exposure_curve: list[float] = []
        core_curve, trade_curve, cash_curve = [], [], []
        # V9.0 M2.3: 闭环成交跟踪(镜像实盘 PositionState 的 entry_ts/peak/trough)
        closed_trades: list[dict[str, Any]] = []
        bucket_open: dict[str, dict[str, Any]] = {}  # bucket -> {entry_ts, entry_price, peak, trough}

        def equity_now(close: float) -> float:
            return ledger.cash() + (
                ledger.qty(self.symbol, "core") + ledger.qty(self.symbol, "trade")
            ) * close

        # 策略信号 -> 次bar意图队列(实盘的 on_signal 等价物)
        async def on_signal(sig) -> None:
            a = analytics.get(self.symbol)
            regime = "SIDEWAY"
            if a is not None:
                regime = a.regime or "SIDEWAY"
            assessment = regime_engine.get(self.symbol)
            if assessment is not None:
                regime = assessment.regime
            if sig.side.value == "BUY":
                equity = equity_now(float(sig.price))
                alpha_score = 60.0
                sizing = sizer.size(
                    decision_score=sig.score,
                    alpha_score=alpha_score,
                    regime=regime,
                    equity=equity,
                    price=float(sig.price),
                    exposure_room_quote=max(
                        0.0, equity * 0.9 - (
                            ledger.qty(self.symbol, "core")
                            + ledger.qty(self.symbol, "trade")
                        ) * float(sig.price),
                    ),
                )
                if sizing["quote"] > 0:
                    next_bar.submit({
                        "side": "BUY", "bucket": "trade",
                        "qty": sizing["quantity"],
                        "strategy": group_of(sig.source_strategy or sig.strategy),
                        "reason": f"decision:{sig.score:.0f} {sizing['detail'][:80]}",
                        "regime": regime,
                    })
            else:
                # 卖出: bucket 闸门(交易仓)
                trade_qty = ledger.qty(self.symbol, "trade")
                sell_qty = min(sig.quantity or trade_qty, trade_qty)
                if sell_qty > 0:
                    next_bar.submit({
                        "side": "SELL", "bucket": "trade",
                        "qty": sell_qty,
                        "strategy": group_of(sig.source_strategy or sig.strategy),
                        "reason": sig.reason_str[:100] if hasattr(sig, "reason_str") else "exit",
                        "regime": regime,
                    })

        strategy_engine.on_signal = on_signal

        for i, k in enumerate(klines):
            open_p, high_p, low_p, close = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            ts = int(k[0])

            # ===== 1. bar 开盘: 执行上一收盘的挂起意图(次bar成交) =====
            if next_bar.pending_count > 0:
                for intent in next_bar.execute_at_open(open_p, slippage):
                    side, qty, bucket = intent["side"], intent["qty"], intent["bucket"]
                    if qty <= 0:
                        continue
                    price = intent["exec_price"]
                    # 现金约束
                    if side == "BUY" and qty * price > ledger.cash():
                        qty = ledger.cash() / price
                    if qty * price < 10:
                        continue
                    fee = qty * price * self.fee_rate
                    strategy = intent.get("strategy", "")
                    entry = ledger.record_fill(ts, self.symbol, bucket, side, qty, price, fee, strategy)
                    trades += 1
                    # V9.0 M2.3: 维护开仓位置, SELL 时产出闭环成交
                    if side == "BUY":
                        if entry.position_before <= 0:
                            bucket_open[bucket] = {
                                "entry_ts": ts,
                                "entry_price": entry.avg_cost_after,
                                "peak": price,
                                "trough": price,
                            }
                    else:
                        op = bucket_open.get(bucket)
                        if op is not None:
                            entry_price = entry.avg_cost_before or op["entry_price"]
                            peak = op.get("peak", price)
                            trough = op.get("trough", price)
                            closed_trades.append({
                                "strategy": strategy,
                                "bucket": bucket,
                                "entry_ts": op["entry_ts"],
                                "exit_ts": ts,
                                "entry_price": entry_price,
                                "exit_price": price,
                                "quantity": qty,
                                "realized_pnl": entry.realized_pnl,
                                "holding_seconds": (ts - op["entry_ts"]) / 1000.0 if op["entry_ts"] > 0 else 0.0,
                                "max_profit": (peak - entry_price) * qty if peak > entry_price > 0 else 0.0,
                                "max_drawdown": (entry_price - trough) * qty if 0 < trough < entry_price else 0.0,
                            })
                            if entry.position_after <= 0:
                                bucket_open.pop(bucket, None)

            # ===== 2. bar 内: 合成 tick 喂指标(OHLC 4 笔) =====
            qv = float(k[5]) / 4
            for price in (open_p, high_p, low_p, close):
                bar_tick_id += 1
                await analytics.on_trade(
                    self.symbol,
                    TradeTick(
                        trade_id=bar_tick_id, symbol=self.symbol, price=price,
                        quantity=qv / price if price > 0 else 0.0,
                        quote_quantity=qv,
                        is_buyer_maker=price < open_p,
                        trade_time=ts,
                    ),
                )
            a = analytics.get(self.symbol)
            if a is None:
                continue

            # ===== 3. 周期: regime + allocation 再平衡(仍按收盘决策/次bar执行) =====
            if i > 0 and i % self.regime_eval_bars == 0:
                btc_close, btc_age = (0.0, 0.0)
                btc_change = 0.0
                btc_trend = "neutral"
                if btc is not None:
                    btc_close, btc_age = btc.close_asof(ts)
                    if btc_age == float("inf"):
                        btc_trend = "neutral"  # 数据不可用, 不降级错误
                    else:
                        lookback = max(0, i - 1440)
                        if btc_close > 0:
                            past, _ = btc.close_asof(int(klines[lookback][0]))
                            if past > 0:
                                btc_change = (btc_close - past) / past * 100
                        btc_trend = "up" if btc_change > 2 else (
                            "down" if btc_change < -2 else "neutral"
                        )

                assessment = regime_engine.evaluate(
                    symbol=self.symbol,
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
                analytics.set_regime(self.symbol, assessment.regime)
                analytics.set_change_24h(self.symbol, 0.0)

                if benchmark_entry_price is None:
                    benchmark_entry_price = close

                volatility = (
                    (a.recent_high - a.recent_low)
                    / ((a.recent_high + a.recent_low) / 2)
                    if a.recent_high > 0 else 0.0
                )
                risk_factor = allocator.risk_adjustment_factor(
                    volatility=volatility,
                    btc_change_24h=btc_change,
                    btc_trend=btc_trend,
                    drawdown=portfolio_drawdown,
                )
                equity = equity_now(close)
                plan = allocator.plan(
                    symbol=self.symbol,
                    regime=assessment.regime,
                    confidence=assessment.confidence,
                    equity=equity,
                    market_price=close,
                    current_core_qty=ledger.qty(self.symbol, "core"),
                    current_trade_qty=ledger.qty(self.symbol, "trade"),
                    risk_factor=risk_factor,
                )
                if plan.rebalance_needed:
                    # 核心仓调整(次bar执行)
                    cur_core = ledger.qty(self.symbol, "core")
                    core_diff = plan.core_diff
                    if core_diff < 0:
                        next_bar.submit({
                            "side": "SELL", "bucket": "core",
                            "qty": min(cur_core, -core_diff),
                            "strategy": "allocation", "reason": "rebalance减仓",
                            "regime": assessment.regime,
                        })
                    elif core_diff > 0:
                        cost = core_diff * close
                        if cost <= ledger.cash() * 0.98:
                            next_bar.submit({
                                "side": "BUY", "bucket": "core",
                                "qty": core_diff, "strategy": "allocation",
                                "reason": "rebalance加仓",
                                "regime": assessment.regime,
                            })
                    rebalances += 1

            # ===== 4. 周期: 跑真实策略(每 N 根, 性能) =====
            if i > 0 and i % self.strategy_eval_bars == 0 and a is not None:
                try:
                    await strategy_engine.on_analytics(self.symbol, a)
                except Exception:
                    self.logger.exception("回测策略执行异常")

            # ===== 5. 曲线与回撤 =====
            equity = equity_now(close)
            equity_curve.append(equity)
            exposure_curve.append(
                ((ledger.qty(self.symbol, "core") + ledger.qty(self.symbol, "trade")) * close / equity)
                if equity > 0 else 0.0
            )
            core_curve.append(ledger.qty(self.symbol, "core"))
            trade_curve.append(ledger.qty(self.symbol, "trade"))
            cash_curve.append(ledger.cash())
            # V9.0 M2.3: 更新开仓位置 peak/trough(本 bar 高低)
            for b, op in bucket_open.items():
                if ledger.qty(self.symbol, b) > 0:
                    op["peak"] = max(op.get("peak", high_p), high_p)
                    op["trough"] = min(op.get("trough", low_p), low_p)
            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            portfolio_drawdown = max(portfolio_drawdown, dd)
            result.max_drawdown = portfolio_drawdown

        # ===== 统计 =====
        final_equity = equity_curve[-1] if equity_curve else self.initial_cash
        last_price = float(klines[-1][4])
        bench_entry = benchmark_entry_price if benchmark_entry_price else float(klines[0][1])

        result.final_equity = final_equity
        result.total_return = (final_equity - self.initial_cash) / self.initial_cash
        result.benchmark_return = (last_price - bench_entry) / bench_entry if bench_entry > 0 else 0.0
        result.excess_return = result.total_return - result.benchmark_return
        result.rebalances = rebalances
        result.trade_count = trades
        result.final_core_qty = ledger.qty(self.symbol, "core")
        result.final_trade_qty = ledger.qty(self.symbol, "trade")
        result.core_contribution = (
            (last_price - ledger.avg_cost(self.symbol, "core")) * result.final_core_qty
            if result.final_core_qty > 0 else 0.0
        )
        trade_realized = sum(
            e.realized_pnl for e in ledger.entries
            if e.bucket == "trade" and e.side == "SELL"
        )
        trade_unrealized = (
            (last_price - ledger.avg_cost(self.symbol, "trade")) * result.final_trade_qty
            if result.final_trade_qty > 0 else 0.0
        )
        result.trade_contribution = trade_realized + trade_unrealized

        recon = ledger.reconcile(self.symbol, ledger.cash(), last_price, self.initial_cash)
        result.reconciliation = recon.to_dict()

        if len(equity_curve) > 2:
            rets = [
                (equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1]
                if equity_curve[j - 1] > 0 else 0.0
                for j in range(1, len(equity_curve))
            ]
            mean_r = sum(rets) / len(rets)
            std = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
            if std > 0:
                result.sharpe = (mean_r / std) * math.sqrt(bars_per_year(self.interval))
            result.sortino = compute_sortino(rets, bars_per_year(self.interval))

        # V9.0 M2.3: Calmar + 闭环成交统计
        result.calmar = compute_calmar(result.total_return, result.max_drawdown)
        trade_metrics = compute_closed_trade_metrics(closed_trades)
        result.win_rate = trade_metrics["win_rate"]
        result.profit_factor = trade_metrics["profit_factor"]
        result.avg_holding_seconds = trade_metrics["avg_holding_seconds"]
        result.closed_trades = trade_metrics["closed_trades"]
        result.attribution = trade_metrics["attribution"]

        result.equity_curve = equity_curve[-200:]
        result.exposure_curve = [round(e, 3) for e in exposure_curve[-200:]]
        result.core_curve = core_curve[-200:]
        result.trade_curve = trade_curve[-200:]
        result.cash_curve = [round(c, 2) for c in cash_curve[-200:]]

        self.logger.info("组合回测完成", **result.summary())
        return result


async def run_portfolio_backtest(
    symbol: str = "SOLUSDT",
    days: int = 7,
    interval: str = "1m",
    with_btc: bool = True,
    slippage_bps: float = 10.0,
) -> PortfolioBacktestResult:
    """便捷入口(V7: 真实策略管线)"""
    klines = await fetch_klines_paged(symbol, interval=interval, days=days)

    btc_klines = None
    if with_btc:
        try:
            btc_klines = await fetch_klines_paged("BTCUSDT", interval=interval, days=days)
        except Exception as e:
            print(f"BTC 数据获取失败(忽略): {e}")

    bt = PortfolioBacktester(
        symbol=symbol, interval=interval, slippage_bps=slippage_bps
    )
    return await bt.run(klines, btc_klines=btc_klines)
