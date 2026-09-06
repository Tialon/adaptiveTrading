"""
Portfolio Backtest(V6.0 — Correctness First)

修复 V5 版四个关键问题:
1. 交易仓真实建立/买卖/再买卖(删除 pass)
2. risk_adjustment_factor 真实计算(BTC 历史数据 + 组合回撤), 与实盘共用同一代码
3. 分页拉取完整历史数据(days=30 真拿 30 天)
4. Regime 用 MarketRegimeEngine(与实盘共用, 不再重写简化版)
5. 双仓成本经 PortfolioLedger 独立记账(trade PnL 不再用 core_cost)
6. Sharpe 按 interval 年化
7. Benchmark 用统一执行价(首个评估时点的 close)

模拟:
    现金 -> 核心仓(70%) -> 交易仓(30%) -> 动态敞口再平衡 -> 收益
"""

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin


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
    final_core_qty: float = 0.0
    final_trade_qty: float = 0.0
    core_contribution: float = 0.0
    trade_contribution: float = 0.0
    reconciliation: dict[str, Any] = field(default_factory=dict)  # V6: 对账
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
            "final_core_qty": round(self.final_core_qty, 4),
            "final_trade_qty": round(self.final_trade_qty, 4),
            "core_pnl": round(self.core_contribution, 2),
            "trade_pnl": round(self.trade_contribution, 2),
            "balanced": self.reconciliation.get("balanced"),
        }


def periods_per_year(interval: str) -> int:
    """V6: 按 interval 年化因子"""
    table = {
        "1m": 525600, "3m": 175200, "5m": 105120, "15m": 35040,
        "30m": 17520, "1h": 8760, "2h": 4380, "4h": 2190,
        "6h": 1460, "12h": 730, "1d": 365,
    }
    return table.get(interval, 525600)


async def fetch_klines_paged(
    symbol: str,
    interval: str = "1m",
    days: int = 7,
    max_bars: int = 43200,  # 30 天 1m 上限
) -> list[list[Any]]:
    """V6: 分页拉取完整历史数据(每页 1000 根)"""
    from at20_market.market_rest_client import BinanceRestClient

    target = min(days * periods_per_year(interval) // 365 if False else days * 1440, max_bars)
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
    """组合回测器(V6: 正确性优先)"""

    def __init__(
        self,
        symbol: str = "SOLUSDT",
        initial_cash: float = 20000.0,
        fee_rate: float = 0.001,
        interval: str = "1m",
        regime_eval_bars: int = 60,
        rebalance_tolerance: float = 0.05,
    ):
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.interval = interval
        self.regime_eval_bars = regime_eval_bars
        self.rebalance_tolerance = rebalance_tolerance

    async def run(
        self,
        klines: list[list[Any]],
        btc_klines: Optional[list[list[Any]]] = None,
    ) -> PortfolioBacktestResult:
        """执行组合回测

        btc_klines: 对齐时间的 BTC K线(V6: 真实 BTC 风险因子), 可选
        """
        from at30_analytics.regime import MarketRegimeEngine
        from at60_risk.risk_allocation import PortfolioAllocator
        from at60_risk.risk_ledger import PortfolioLedger

        result = PortfolioBacktestResult(symbol=self.symbol, bars=len(klines))
        if len(klines) < 100:
            self.logger.warning("K线不足", bars=len(klines))
            return result

        allocator = PortfolioAllocator(
            initial_equity=self.initial_cash,
            rebalance_tolerance=self.rebalance_tolerance,
        )
        regime_engine = MarketRegimeEngine()  # V6: 与实盘共用
        ledger = PortfolioLedger()
        ledger.init_cash(self.initial_cash)

        # BTC 时间索引(用 close 序列按 bar 对齐)
        btc_closes: list[float] = []
        if btc_klines:
            btc_by_ts = {int(k[0]): float(k[4]) for k in btc_klines}
            btc_closes = [btc_by_ts.get(int(k[0]), 0.0) or (btc_closes[-1] if btc_closes else 0.0) for k in klines]

        # EMA(供 RegimeEngine 输入)
        ema_fast = ema_slow = None
        recent_high = recent_low = 0.0
        peak_equity = self.initial_cash
        portfolio_drawdown = 0.0

        rebalances = 0
        trades = 0
        benchmark_entry_price: Optional[float] = None  # 统一执行价起点
        last_trade_price = 0.0

        equity_curve, exposure_curve = [], []
        core_curve, trade_curve, cash_curve = [], [], []

        def equity_now(close: float) -> float:
            return ledger.cash() + (
                ledger.qty(self.symbol, "core") + ledger.qty(self.symbol, "trade")
            ) * close

        for i, k in enumerate(klines):
            close = float(k[4])
            high, low = float(k[2]), float(k[3])
            ts = int(k[0])

            # EMA
            if ema_fast is None:
                ema_fast = ema_slow = close
                recent_high, recent_low = high, low
            else:
                ema_fast = close * (2 / 13) + ema_fast * (11 / 13)
                ema_slow = close * (2 / 27) + ema_slow * (25 / 27)
            recent_high = max(recent_high, high)
            recent_low = min(recent_low, low)
            # 衰减窗口(约 120 根)
            if i % 120 == 0 and i > 0:
                recent_high *= 0.999
                recent_low = min(recent_low * 1.001, close)

            # ---- 周期评估 regime(共用实盘 MarketRegimeEngine) ----
            if i > 0 and i % self.regime_eval_bars == 0:
                # 窗口涨跌幅
                lookback = max(0, i - 1440)
                chg = (close - float(klines[lookback][4])) / float(klines[lookback][4])
                # BTC 环境(真实数据)
                btc_change = btc_trend = 0.0
                btc_trend_str = "neutral"
                if btc_closes:
                    btc_now = btc_closes[i]
                    btc_past = btc_closes[lookback]
                    if btc_past > 0:
                        btc_change = (btc_now - btc_past) / btc_past * 100
                    btc_trend_str = "up" if btc_change > 2 else ("down" if btc_change < -2 else "neutral")

                assessment = regime_engine.evaluate(
                    symbol=self.symbol,
                    symbol_trend="up" if ema_fast > ema_slow * 1.002 else (
                        "down" if ema_fast < ema_slow * 0.998 else "neutral"
                    ),
                    symbol_ema_fast=ema_fast,
                    symbol_ema_slow=ema_slow,
                    recent_high=recent_high,
                    recent_low=recent_low,
                    volume_ratio=1.0,
                    delta_ratio=chg / 3 if abs(chg) < 0.3 else 0.0,
                    cvd_rising=chg > 0,
                    btc_trend=btc_trend_str,
                    btc_change_24h=btc_change,
                )

                # V6: 真实风险因子(BTC + 组合回撤)
                volatility = (recent_high - recent_low) / ((recent_high + recent_low) / 2) if recent_high > 0 else 0.0
                risk_factor = allocator.risk_adjustment_factor(
                    volatility=volatility,
                    btc_change_24h=btc_change,
                    btc_trend=btc_trend_str,
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

                # 统一 benchmark 起点(首次评估时点)
                if benchmark_entry_price is None:
                    benchmark_entry_price = close

                # ---- 再平衡 ----
                if plan.rebalance_needed:
                    target_core = plan.target_core_qty
                    target_trade = plan.target_trade_qty
                    cur_core = ledger.qty(self.symbol, "core")
                    cur_trade = ledger.qty(self.symbol, "trade")

                    core_diff = target_core - cur_core
                    trade_diff = target_trade - cur_trade
                    # 先减后加(卖出释放现金)
                    if core_diff < 0:
                        qty = min(cur_core, -core_diff)
                        ledger.record_fill(ts, self.symbol, "core", "SELL", qty, close, qty * close * self.fee_rate)
                        trades += 1
                    if trade_diff < 0:
                        qty = min(cur_trade, -trade_diff)
                        ledger.record_fill(ts, self.symbol, "trade", "SELL", qty, close, qty * close * self.fee_rate)
                        trades += 1
                    if core_diff > 0:
                        cost = core_diff * close * (1 + self.fee_rate)
                        if cost <= ledger.cash():
                            ledger.record_fill(ts, self.symbol, "core", "BUY", core_diff, close, core_diff * close * self.fee_rate)
                            trades += 1
                    if trade_diff > 0:
                        cost = trade_diff * close * (1 + self.fee_rate)
                        if cost <= ledger.cash():
                            ledger.record_fill(ts, self.symbol, "trade", "BUY", trade_diff, close, trade_diff * close * self.fee_rate)
                            trades += 1
                            last_trade_price = close
                    rebalances += 1

            # ---- 交易仓高抛低吸(V6: 真实建立与循环) ----
            trade_qty_now = ledger.qty(self.symbol, "trade")
            if trade_qty_now == 0 and ledger.cash() > close * 10:
                # 建交易仓: 用 Allocation 计划的 trade 目标(简化: 权益 15%)
                equity = equity_now(close)
                buy_quote = equity * 0.15
                buy_qty = min(buy_quote / close, ledger.cash() / (close * (1 + self.fee_rate)))
                if buy_qty * close >= 10:
                    ledger.record_fill(ts, self.symbol, "trade", "BUY", buy_qty, close, buy_qty * close * self.fee_rate)
                    last_trade_price = close
                    trades += 1
            elif trade_qty_now > 0 and last_trade_price > 0:
                if close >= last_trade_price * 1.03:
                    sell = trade_qty_now * 0.3
                    if sell * close >= 10:
                        ledger.record_fill(ts, self.symbol, "trade", "SELL", sell, close, sell * close * self.fee_rate)
                        trades += 1
                        last_trade_price = close
                elif close <= last_trade_price * 0.97:
                    # 低吸: 回补交易仓(现金允许)
                    rebuy_quote = min(equity_now(close) * 0.05, ledger.cash() * 0.5)
                    rebuy_qty = rebuy_quote / close
                    if rebuy_qty * close >= 10:
                        ledger.record_fill(ts, self.symbol, "trade", "BUY", rebuy_qty, close, rebuy_qty * close * self.fee_rate)
                        trades += 1
                        last_trade_price = close

            # ---- 曲线与回撤 ----
            equity = equity_now(close)
            equity_curve.append(equity)
            exposure_curve.append(
                ((ledger.qty(self.symbol, "core") + ledger.qty(self.symbol, "trade")) * close / equity)
                if equity > 0 else 0.0
            )
            core_curve.append(ledger.qty(self.symbol, "core"))
            trade_curve.append(ledger.qty(self.symbol, "trade"))
            cash_curve.append(ledger.cash())
            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            portfolio_drawdown = max(portfolio_drawdown, dd)
            result.max_drawdown = portfolio_drawdown

        # ---- 统计 ----
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
        # 贡献分解(双仓独立成本, 经账本)
        result.core_contribution = (
            (last_price - ledger.avg_cost(self.symbol, "core")) * result.final_core_qty
            if result.final_core_qty > 0 else 0.0
        )
        # 交易仓贡献 = 已实现(全部条目 trade SELL) + 未实现
        trade_realized = sum(
            e.realized_pnl for e in ledger.entries
            if e.bucket == "trade" and e.side == "SELL"
        )
        trade_unrealized = (
            (last_price - ledger.avg_cost(self.symbol, "trade")) * result.final_trade_qty
            if result.final_trade_qty > 0 else 0.0
        )
        result.trade_contribution = trade_realized + trade_unrealized

        # V6: 对账(账本推演现金 vs 实际现金路径)
        recon = ledger.reconcile(self.symbol, ledger.cash(), last_price, self.initial_cash)
        result.reconciliation = recon.to_dict()

        # 夏普(按 interval 年化)
        if len(equity_curve) > 2:
            rets = [
                (equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1]
                if equity_curve[j - 1] > 0 else 0.0
                for j in range(1, len(equity_curve))
            ]
            mean_r = sum(rets) / len(rets)
            std = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
            if std > 0:
                result.sharpe = (mean_r / std) * math.sqrt(periods_per_year(self.interval))

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
) -> PortfolioBacktestResult:
    """便捷入口(V6: 分页完整数据 + BTC 对齐)"""
    klines = await fetch_klines_paged(symbol, interval=interval, days=days)

    btc_klines = None
    if with_btc:
        try:
            btc_klines = await fetch_klines_paged("BTCUSDT", interval=interval, days=days)
        except Exception as e:
            print(f"BTC 数据获取失败(忽略, risk factor 降级): {e}")

    bt = PortfolioBacktester(symbol=symbol, interval=interval)
    return await bt.run(klines, btc_klines=btc_klines)
