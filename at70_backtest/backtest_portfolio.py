"""
Portfolio Backtest(V5.0)

验证资产管理模型(而非交易信号)的回测:

    现金 -> 核心仓(70%) -> 交易仓(30%) -> 动态敞口再平衡 -> 收益

模拟:
- 每 N 根 K 线评估一次 regime(用 EMA 趋势 + 波动率近似)
- Allocation 引擎给出目标敞口 -> 偏离容忍带触发再平衡
- 交易仓做简单高抛低吸(网格近似: 跌 x% 买 / 涨 y% 卖)
- 核心仓只在再平衡时调整(交易仓卖出永不动核心仓)

输出对比:
- Strategy(core+trade 双仓动态)
- SOL Buy-Hold(基准)
- 曲线: equity / exposure / core_qty / trade_qty / cash
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
    benchmark_return: float = 0.0  # Buy-Hold
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    final_core_qty: float = 0.0
    final_trade_qty: float = 0.0
    core_contribution: float = 0.0  # 核心仓贡献 PnL
    trade_contribution: float = 0.0  # 交易仓贡献 PnL(含已实现)
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
        }


class PortfolioBacktester(LoggerMixin):
    """组合回测器(资产管理模型验证)"""

    def __init__(
        self,
        symbol: str = "SOLUSDT",
        initial_cash: float = 20000.0,
        fee_rate: float = 0.001,
        regime_eval_bars: int = 60,  # 每 60 根(1小时)评估一次 regime
        rebalance_tolerance: float = 0.05,
    ):
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.fee_rate = fee_rate
        self.regime_eval_bars = regime_eval_bars
        self.rebalance_tolerance = rebalance_tolerance

    async def run(self, klines: list[list[Any]]) -> PortfolioBacktestResult:
        """执行组合回测"""
        from at60_risk.risk_allocation import PortfolioAllocator

        result = PortfolioBacktestResult(symbol=self.symbol, bars=len(klines))
        if len(klines) < 100:
            self.logger.warning("K线不足", bars=len(klines))
            return result

        allocator = PortfolioAllocator(
            initial_equity=self.initial_cash,
            rebalance_tolerance=self.rebalance_tolerance,
        )

        cash = self.initial_cash
        core_qty = 0.0
        trade_qty = 0.0
        core_cost = 0.0
        trade_realized = 0.0  # 交易仓累计已实现
        rebalances = 0
        trades = 0

        # EMA 状态(近似 regime 判定)
        ema_fast = ema_slow = None
        peak_equity = self.initial_cash
        last_trade_price = 0.0  # 交易仓上次成交价(高抛低吸锚)

        equity_curve, exposure_curve = [], []
        core_curve, trade_curve, cash_curve = [], [], []

        def regime_of(ema_f, ema_s, volatility, chg_pct) -> tuple[str, float]:
            """近似 regime(与实盘 RegimeEngine 语义对齐)"""
            if ema_f is None or ema_s is None:
                return "SIDEWAY", 0.45
            spread = (ema_f - ema_s) / ema_s if ema_s > 0 else 0
            votes = 0
            votes += 1 if spread > 0.002 else (-1 if spread < -0.002 else 0)
            votes += 1 if chg_pct > 2 else (-1 if chg_pct < -2 else 0)
            if volatility > 0.03 and votes <= -1:
                return "PANIC", 0.7
            if votes >= 2:
                return "BULL", 0.75
            if votes <= -2:
                return "BEAR", 0.7
            return "SIDEWAY", 0.45

        for i, k in enumerate(klines):
            close = float(k[4])
            vol = (float(k[2]) - float(k[3])) / close if close > 0 else 0.0

            # EMA 更新
            if ema_fast is None:
                ema_fast = ema_slow = close
            else:
                ema_fast = close * (2 / 13) + ema_fast * (11 / 13)
                ema_slow = close * (2 / 27) + ema_slow * (25 / 27)

            # 周期评估 regime + 再平衡
            if i > 0 and i % self.regime_eval_bars == 0:
                # 24h 涨跌幅近似(窗口内)
                lookback = max(0, i - 1440)
                chg = (close - float(klines[lookback][4])) / float(klines[lookback][4]) * 100
                regime, confidence = regime_of(ema_fast, ema_slow, vol, chg)
                risk_factor = allocator.risk_adjustment_factor(
                    volatility=vol, btc_change_24h=0.0, btc_trend="neutral", drawdown=0.0
                )

                equity = cash + (core_qty + trade_qty) * close
                plan = allocator.plan(
                    symbol=self.symbol, regime=regime, confidence=confidence,
                    equity=equity, market_price=close,
                    current_core_qty=core_qty, current_trade_qty=trade_qty,
                    risk_factor=risk_factor,
                )

                # 执行再平衡(偏离容忍带)
                if plan.rebalance_needed:
                    target_total = plan.target_core_qty + plan.target_trade_qty
                    diff = target_total - (core_qty + trade_qty)
                    if diff > 0 and cash > diff * close * 1.001:
                        # 加仓: 按双仓比例
                        core_add = plan.core_diff if plan.core_diff > 0 else 0
                        trade_add = plan.trade_diff if plan.trade_diff > 0 else 0
                        buy_total = core_add + trade_add
                        if buy_total > 0 and buy_total * close <= cash:
                            fee = buy_total * close * self.fee_rate
                            cash -= buy_total * close + fee
                            if core_add > 0:
                                core_cost = (core_cost * core_qty + core_add * close) / (core_qty + core_add)
                                core_qty += core_add
                            trade_qty += trade_add
                            trades += 1
                            rebalances += 1
                    elif diff < 0:
                        # 减仓: 先减交易仓, 不够再减核心仓
                        sell_total = -diff
                        sell_trade = min(trade_qty, sell_total)
                        sell_core = min(core_qty, sell_total - sell_trade)
                        if sell_trade + sell_core > 0:
                            trade_realized += sell_trade * (close - core_cost)  # 简化: 用 core_cost 近似
                            proceeds = (sell_trade + sell_core) * close * (1 - self.fee_rate)
                            cash += proceeds
                            trade_qty -= sell_trade
                            core_qty -= sell_core
                            trades += 1
                            rebalances += 1

            # 交易仓高抛低吸(网格近似)
            if trade_qty > 0 and last_trade_price > 0 and close > last_trade_price * 1.03:
                sell = trade_qty * 0.3  # 涨3% 卖 30% 交易仓
                if sell * close >= 10:
                    trade_realized += sell * (close - core_cost)
                    cash += sell * close * (1 - self.fee_rate)
                    trade_qty -= sell
                    last_trade_price = close
                    trades += 1
            elif last_trade_price == 0 or (trade_qty == 0 and cash > close * 10):
                # 建交易仓(初始)
                pass

            # 曲线
            equity = cash + (core_qty + trade_qty) * close
            equity_curve.append(equity)
            exposure_curve.append(((core_qty + trade_qty) * close / equity) if equity > 0 else 0.0)
            core_curve.append(core_qty)
            trade_curve.append(trade_qty)
            cash_curve.append(cash)
            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            result.max_drawdown = max(result.max_drawdown, dd)

        # 统计
        first_price = float(klines[0][1])
        last_price = float(klines[-1][4])
        final_equity = equity_curve[-1] if equity_curve else self.initial_cash

        result.final_equity = final_equity
        result.total_return = (final_equity - self.initial_cash) / self.initial_cash
        result.benchmark_return = (last_price - first_price) / first_price if first_price > 0 else 0.0
        result.excess_return = result.total_return - result.benchmark_return
        result.rebalances = rebalances
        result.trade_count = trades
        result.final_core_qty = core_qty
        result.final_trade_qty = trade_qty
        result.core_contribution = core_qty * (last_price - core_cost) if core_qty > 0 else 0.0
        result.trade_contribution = trade_realized

        # 夏普
        if len(equity_curve) > 2:
            rets = [
                (equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1]
                if equity_curve[j - 1] > 0 else 0.0
                for j in range(1, len(equity_curve))
            ]
            mean_r = sum(rets) / len(rets)
            std = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
            if std > 0:
                result.sharpe = (mean_r / std) * math.sqrt(365 * 24 * 60)

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
) -> PortfolioBacktestResult:
    """便捷入口"""
    from at20_market.market_rest_client import BinanceRestClient

    client = BinanceRestClient()
    await client.connect()
    try:
        limit = min(days * 1440, 1000)
        klines = await client.get_klines(symbol, interval=interval, limit=limit)
    finally:
        await client.disconnect()

    bt = PortfolioBacktester(symbol=symbol)
    return await bt.run(klines)
