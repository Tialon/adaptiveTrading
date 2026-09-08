"""
订单流与量价指标

- VWAP   : 滚动成交量加权平均价
- Delta  : 主动买卖量差(滚动窗口)
- CVD    : 累计成交量差(Cumulative Volume Delta)
"""

from collections import deque
from dataclasses import dataclass

from at20_market.market_models import TradeTick


@dataclass
class VWAPResult:
    """VWAP 计算结果"""

    vwap: float = 0.0
    upper_band: float = 0.0  # vwap + 2σ
    lower_band: float = 0.0  # vwap - 2σ
    deviation: float = 0.0  # 当前价相对 vwap 的偏离率


@dataclass
class DeltaResult:
    """Delta 计算结果"""

    buy_volume: float = 0.0
    sell_volume: float = 0.0
    delta: float = 0.0  # buy - sell
    delta_ratio: float = 0.0  # delta / total, [-1, 1]


@dataclass
class CVDResult:
    """CVD 计算结果"""

    cvd: float = 0.0
    cvd_slope: float = 0.0  # 近期斜率(每笔)
    rising: bool = False
    falling: bool = False


class VWAPCalculator:
    """滚动 VWAP(含标准差通道)"""

    def __init__(self, window: int = 300):
        self.window = window
        self._pv: deque[float] = deque(maxlen=window)  # price * volume
        self._v: deque[float] = deque(maxlen=window)
        self._p2v: deque[float] = deque(maxlen=window)  # price^2 * volume

    def update(self, price: float, quantity: float) -> VWAPResult:
        """喂入一笔成交,返回当前 VWAP"""
        q = quantity if quantity > 0 else 1e-12
        self._pv.append(price * q)
        self._v.append(q)
        self._p2v.append(price * price * q)

        total_v = sum(self._v)
        if total_v <= 0:
            return VWAPResult()

        vwap = sum(self._pv) / total_v
        # 量加权方差
        var = max(0.0, sum(self._p2v) / total_v - vwap * vwap)
        std = var ** 0.5
        return VWAPResult(
            vwap=vwap,
            upper_band=vwap + 2 * std,
            lower_band=vwap - 2 * std,
            deviation=0.0,  # 由调用方结合最新价填充
        )

    def deviation_of(self, price: float) -> VWAPResult:
        """以指定价格计算偏离率(不追加新样本)"""
        total_v = sum(self._v)
        if total_v <= 0:
            return VWAPResult()
        vwap = sum(self._pv) / total_v
        var = max(0.0, sum(self._p2v) / total_v - vwap * vwap)
        std = var ** 0.5
        return VWAPResult(
            vwap=vwap,
            upper_band=vwap + 2 * std,
            lower_band=vwap - 2 * std,
            deviation=(price - vwap) / vwap if vwap > 0 else 0.0,
        )


class DeltaTracker:
    """滚动窗口买卖量差"""

    def __init__(self, window: int = 300):
        self.window = window
        self._ticks: deque[TradeTick] = deque(maxlen=window)

    def update(self, tick: TradeTick) -> DeltaResult:
        """喂入成交,返回窗口内 Delta"""
        self._ticks.append(tick)
        buy = sum(t.quote_quantity for t in self._ticks if not t.is_buyer_maker)
        sell = sum(t.quote_quantity for t in self._ticks if t.is_buyer_maker)
        total = buy + sell
        delta = buy - sell
        return DeltaResult(
            buy_volume=buy,
            sell_volume=sell,
            delta=delta,
            delta_ratio=delta / total if total > 0 else 0.0,
        )


class CVDTracker:
    """累计成交量差(带斜率)"""

    def __init__(self, slope_window: int = 50, max_len: int = 5000):
        self.slope_window = slope_window
        self._cvd_series: deque[float] = deque(maxlen=max_len)
        self._cvd = 0.0

    def update(self, tick: TradeTick) -> CVDResult:
        """喂入成交,更新 CVD"""
        signed = -tick.quote_quantity if tick.is_buyer_maker else tick.quote_quantity
        self._cvd += signed
        self._cvd_series.append(self._cvd)

        w = list(self._cvd_series)[-self.slope_window:]
        if len(w) >= 2:
            slope = (w[-1] - w[0]) / (len(w) - 1)
        else:
            slope = 0.0
        return CVDResult(cvd=self._cvd, cvd_slope=slope, rising=slope > 0, falling=slope < 0)

    def reset(self) -> None:
        """重置累计值"""
        self._cvd = 0.0
        self._cvd_series.clear()
