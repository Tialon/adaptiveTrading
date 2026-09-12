"""
Walk-Forward 回测(V3.0)

避免过拟合的滚动窗口验证:

    [--- 训练 ---][-- 验证 --] -> 输出该窗口绩效
          [--- 训练 ---][-- 验证 --] -> ...
                [--- 训练 ---][-- 验证 --]

每个窗口:
- 训练段: 阈值扫描(入场阈值网格)
- 验证段: 用调优后参数模拟交易, 记录绩效

汇总: 各窗口验证收益一致性 -> 过拟合检测(训练/验证收益间隙)
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from at01_common.logger import LoggerMixin


@dataclass
class WindowResult:
    """单个 walk-forward 窗口结果"""

    index: int
    train_start: int = 0  # bar 序号
    train_end: int = 0
    test_start: int = 0
    test_end: int = 0
    best_threshold: float = 80.0
    train_return: float = 0.0
    test_return: float = 0.0
    test_trades: int = 0
    test_win_rate: float = 0.0
    test_max_drawdown: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.index,
            "bars": f"{self.test_start}-{self.test_end}",
            "best_threshold": self.best_threshold,
            "train_return": round(self.train_return, 4),
            "test_return": round(self.test_return, 4),
            "trades": self.test_trades,
            "win_rate": round(self.test_win_rate, 3),
            "max_drawdown": round(self.test_max_drawdown, 4),
        }


@dataclass
class WalkForwardResult:
    """汇总"""

    symbol: str = ""
    windows: list[WindowResult] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        """验证段收益是否稳定(>60% 窗口为正且平均为正)"""
        if not self.windows:
            return False
        positive = sum(1 for w in self.windows if w.test_return > 0)
        return positive / len(self.windows) > 0.6 and self.avg_test_return > 0

    @property
    def avg_test_return(self) -> float:
        if not self.windows:
            return 0.0
        return sum(w.test_return for w in self.windows) / len(self.windows)

    @property
    def overfit_gap(self) -> float:
        """过拟合间隙: 训练平均收益 - 验证平均收益(大 = 过拟合)"""
        if not self.windows:
            return 0.0
        avg_train = sum(w.train_return for w in self.windows) / len(self.windows)
        return avg_train - self.avg_test_return

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "windows": len(self.windows),
            "avg_test_return": round(self.avg_test_return, 4),
            "overfit_gap": round(self.overfit_gap, 4),
            "consistent": self.consistent,
            "verdict": "稳健" if self.consistent and self.overfit_gap < 0.10 else
                       ("过拟合风险" if self.overfit_gap >= 0.10 else "不稳定"),
            "detail": [w.to_dict() for w in self.windows],
        }


class WalkForwardBacktester(LoggerMixin):
    """Walk-Forward 回测器(async)"""

    def __init__(
        self,
        symbol: str,
        initial_cash: float = 10000.0,
        train_bars: int = 300,
        test_bars: int = 100,
        step_bars: int = 100,
        threshold_grid: Optional[list[float]] = None,
    ):
        self.symbol = symbol
        self.initial_cash = initial_cash
        self.train_bars = train_bars
        self.test_bars = test_bars
        self.step_bars = step_bars
        self.threshold_grid = threshold_grid or [60.0, 70.0, 80.0, 85.0]

    async def run(self, klines: list[list[Any]]) -> WalkForwardResult:
        """执行 walk-forward"""
        result = WalkForwardResult(symbol=self.symbol)
        n = len(klines)
        window_idx = 0
        start = 0

        while start + self.train_bars + self.test_bars <= n:
            window_idx += 1
            train = klines[start : start + self.train_bars]
            test = klines[start + self.train_bars : start + self.train_bars + self.test_bars]

            best_threshold, best_train_ret = await self._scan_thresholds(train)
            metrics = await self._simulate(test, best_threshold)

            result.windows.append(
                WindowResult(
                    index=window_idx,
                    train_start=start,
                    train_end=start + self.train_bars,
                    test_start=start + self.train_bars,
                    test_end=start + self.train_bars + self.test_bars,
                    best_threshold=best_threshold,
                    train_return=best_train_ret,
                    test_return=metrics["return"],
                    test_trades=metrics["trades"],
                    test_win_rate=metrics["win_rate"],
                    test_max_drawdown=metrics["max_drawdown"],
                )
            )
            start += self.step_bars

        self.logger.info("Walk-Forward 完成", **result.summary())
        return result

    async def _scan_thresholds(self, klines: list[list[Any]]) -> tuple[float, float]:
        """训练段: 扫描入场阈值"""
        best_threshold = self.threshold_grid[0]
        best_ret = -1e9
        for threshold in self.threshold_grid:
            metrics = await self._simulate(klines, threshold)
            if metrics["return"] > best_ret:
                best_ret = metrics["return"]
                best_threshold = threshold
        return best_threshold, best_ret

    async def _simulate(self, klines: list[list[Any]], threshold: float) -> dict[str, Any]:
        """模拟: K线 OHLC 合成 tick -> 指标 -> 阈值入场 -> 5% 止盈"""
        from at10_market.market_models import TradeTick
        from at20_analytics.engine import AnalyticsEngine
        from at60_execution.execution_paper_broker import PaperBroker

        analytics = AnalyticsEngine(symbols=[self.symbol])
        broker = PaperBroker(initial_cash=self.initial_cash, fee_rate=0.001)

        qty = 0.0
        cost = 0.0
        wins = losses = 0
        curve: list[float] = []
        peak = self.initial_cash
        max_dd = 0.0

        tick_id = 0
        for k in klines:
            open_p, high_p, low_p, close_p, vol = (
                float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]),
            )
            ts = int(k[0])
            qv = vol / 4
            for price in (open_p, high_p, low_p, close_p):
                tick_id += 1
                await analytics.on_trade(
                    self.symbol,
                    TradeTick(
                        trade_id=tick_id, symbol=self.symbol, price=price,
                        quantity=qv / price if price > 0 else 0.0,
                        quote_quantity=qv,
                        is_buyer_maker=price < open_p,
                        trade_time=ts,
                    ),
                )
            a = analytics.get(self.symbol)
            if a is None:
                continue
            equity = broker.cash + qty * a.price
            curve.append(equity)
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)

            # 卖出: 止盈 5%
            if qty > 0 and cost > 0:
                profit = (a.price - cost) / cost
                if profit >= 0.05:
                    order = await broker.create_order(
                        self.symbol, "SELL", "MARKET", qty, None, a.price
                    )
                    if order.status == "FILLED":
                        pnl = (order.avg_fill_price - cost) * qty
                        if pnl > 0:
                            wins += 1
                        else:
                            losses += 1
                        qty = 0.0
                        cost = 0.0
                continue

            # 买入: 评分 >= threshold
            if qty <= 0:
                score = 50.0
                if a.vwap > 0:
                    dip = max(0.0, -a.vwap_deviation)
                    score += min(30.0, dip / 0.02 * 30)
                if a.cvd_rising:
                    score += 15.0
                if a.delta_ratio > 0.1:
                    score += 10.0
                if score >= threshold:
                    quote = min(broker.cash * 0.95, 1000.0)
                    if quote >= 10:
                        order = await broker.create_order(
                            self.symbol, "BUY", "MARKET", quote / a.price, None, a.price
                        )
                        if order.status == "FILLED":
                            qty = order.filled_quantity
                            cost = order.avg_fill_price

        trades = wins + losses
        final = curve[-1] if curve else self.initial_cash
        return {
            "return": (final - self.initial_cash) / self.initial_cash,
            "trades": trades,
            "win_rate": wins / trades if trades > 0 else 0.0,
            "max_drawdown": max_dd,
        }


async def run_walkforward(
    symbol: str = "SOLUSDT",
    days: int = 7,
    interval: str = "1m",
) -> WalkForwardResult:
    """便捷入口: 拉取历史数据并执行"""
    from at10_market.market_rest_client import BinanceRestClient

    client = BinanceRestClient()
    await client.connect()
    try:
        limit = min(days * 1440, 1000)
        klines = await client.get_klines(symbol, interval=interval, limit=limit)
    finally:
        await client.disconnect()

    bt = WalkForwardBacktester(symbol=symbol)
    return await bt.run(klines)
