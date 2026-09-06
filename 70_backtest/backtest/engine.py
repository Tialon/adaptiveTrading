"""
回测引擎(V2.0)

历史 K线 -> 合成 tick 驱动 AnalyticsEngine -> 策略 -> 风控 -> PaperBroker
输出: 收益率 / 胜率 / 最大回撤 / 夏普比率 / 交易次数

用法:
    python -m backtest.run --symbol SOLUSDT --days 7
"""

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from common.utils.logger import LoggerMixin


@dataclass
class BacktestConfig:
    """回测配置"""

    symbol: str = "SOLUSDT"
    interval: str = "1m"
    days: int = 7
    initial_cash: float = 10000.0
    fee_rate: float = 0.001
    entry_threshold: float = 80.0


@dataclass
class BacktestResult:
    """回测结果"""

    symbol: str = ""
    bars: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_return: float = 0.0  # 收益率
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    trade_records: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
            "total_return": round(self.total_return, 4),
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe": round(self.sharpe, 3),
        }


class BacktestEngine(LoggerMixin):
    """回测引擎

    简化模型: 每根 K 线生成收盘 tick 驱动指标,
    用 K 线 OHLC 近似 VWAP(以 close 为价、以 volume 为量的滚动窗口)。
    """

    def __init__(self, config: BacktestConfig):
        self.config = config
        from common.config.settings import get_settings

        # 回测不落库: 覆盖数据库 URL 为内存 SQLite
        import os

        os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    async def run(self, klines: Optional[list[list[Any]]] = None) -> BacktestResult:
        """执行回测

        klines: [openTime, open, high, low, close, volume, closeTime, quoteVol, trades, ...]
        未提供时从币安 REST 拉取。
        """
        from market.models import TradeTick
        from analytics.engine import AnalyticsEngine
        from execution.paper_broker import PaperBroker

        if klines is None:
            klines = await self._fetch_klines()

        result = BacktestResult(symbol=self.config.symbol, bars=len(klines))
        if len(klines) < 50:
            self.logger.warning("K线样本不足", bars=len(klines))
            return result

        # 构建组件(内存模式)
        analytics = AnalyticsEngine(symbols=[self.config.symbol])
        broker = PaperBroker(
            initial_cash=self.config.initial_cash, fee_rate=self.config.fee_rate
        )

        # 简单状态: 持仓
        position_qty = 0.0
        position_cost = 0.0
        position_strategy = ""
        wins = losses = 0
        equity_curve: list[float] = []
        peak_equity = self.config.initial_cash

        # 阈值(与实盘 Entry 一致的简化评分)
        buy_threshold = self.config.entry_threshold

        async def on_analytics(symbol: str, a) -> None:
            nonlocal position_qty, position_cost, position_strategy, wins, losses

            equity = broker.cash + position_qty * a.price
            equity_curve.append(equity)
            nonlocal_peak = peak_equity_local[0]
            if equity > nonlocal_peak:
                peak_equity_local[0] = equity
            dd = (peak_equity_local[0] - equity) / peak_equity_local[0]
            if dd > result.max_drawdown:
                result.max_drawdown = dd

            # ---- 卖出规则(exit 简化版: 止盈 + 移动止盈) ----
            if position_qty > 0 and position_cost > 0:
                profit = (a.price - position_cost) / position_cost
                if profit >= 0.05:
                    order = await broker.create_order(
                        symbol, "SELL", "MARKET", position_qty, None, a.price
                    )
                    if order.status == "FILLED":
                        pnl = (order.avg_fill_price - position_cost) * position_qty
                        result.trade_records.append(
                            {"side": "SELL", "price": order.avg_fill_price,
                             "qty": position_qty, "pnl": round(pnl, 2),
                             "strategy": position_strategy, "reason": f"止盈{profit:.1%}"}
                        )
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1
                        position_qty = 0.0
                        position_cost = 0.0
                    return

            # ---- 买入规则(评分简化: 折价+资金流) ----
            if position_qty <= 0:
                score = 50.0
                if a.vwap > 0:
                    dip = max(0.0, -a.vwap_deviation)
                    score += min(30.0, dip / 0.02 * 30)
                if a.cvd_rising:
                    score += 15.0
                if a.delta_ratio > 0.1:
                    score += 10.0
                if score >= buy_threshold:
                    quote = min(broker.cash * 0.95, 1000.0)
                    if quote >= 10:
                        qty = quote / a.price
                        order = await broker.create_order(
                            symbol, "BUY", "MARKET", qty, None, a.price
                        )
                        if order.status == "FILLED":
                            position_qty = order.filled_quantity
                            position_cost = order.avg_fill_price
                            position_strategy = "backtest_entry"
                            result.trade_records.append(
                                {"side": "BUY", "price": order.avg_fill_price,
                                 "qty": qty, "pnl": 0.0, "strategy": "entry",
                                 "reason": f"score={score:.0f}"}
                            )

        peak_equity_local = [self.config.initial_cash]
        analytics.on_analytics = on_analytics

        # 回放: 每根 K 线按 OHLC 合成 4 笔 tick
        tick_id = 0
        for k in klines:
            open_p, high_p, low_p, close_p, vol = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            ts = int(k[0])
            quarter_vol = vol / 4
            for price in (open_p, high_p, low_p, close_p):
                tick_id += 1
                tick = TradeTick(
                    trade_id=tick_id,
                    symbol=self.config.symbol,
                    price=price,
                    quantity=quarter_vol / price if price > 0 else 0.0,
                    quote_quantity=quarter_vol,
                    is_buyer_maker=price < open_p,  # 简化: 低于开盘视为卖主动
                    trade_time=ts,
                )
                await analytics.on_trade(self.config.symbol, tick)

        # ---- 统计 ----
        final_equity = equity_curve[-1] if equity_curve else self.config.initial_cash
        result.trades = wins + losses
        result.wins = wins
        result.losses = losses
        result.win_rate = wins / result.trades if result.trades > 0 else 0.0
        result.total_pnl = final_equity - self.config.initial_cash
        result.total_return = result.total_pnl / self.config.initial_cash
        result.equity_curve = equity_curve[-200:]

        # 夏普(按 bar 收益率,年化按分钟K线)
        if len(equity_curve) > 2:
            rets = [
                (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
                if equity_curve[i - 1] > 0 else 0.0
                for i in range(1, len(equity_curve))
            ]
            mean_ret = sum(rets) / len(rets)
            std = math.sqrt(sum((r - mean_ret) ** 2 for r in rets) / len(rets))
            bars_per_year = 365 * 24 * 60  # 1m K线
            if std > 0:
                result.sharpe = (mean_ret / std) * math.sqrt(bars_per_year)

        self.logger.info("回测完成", **result.summary())
        return result

    async def _fetch_klines(self) -> list[list[Any]]:
        """拉取历史 K 线"""
        from market.rest_client import BinanceRestClient

        client = BinanceRestClient()
        await client.connect()
        try:
            limit = min(self.config.days * 1440, 1000)
            return await client.get_klines(
                self.config.symbol, interval=self.config.interval, limit=limit
            )
        finally:
            await client.disconnect()


async def run_backtest(config: BacktestConfig) -> BacktestResult:
    """便捷入口"""
    engine = BacktestEngine(config)
    return await engine.run()
