"""
分析引擎

汇总所有分析指标,每个标的产出统一的市场分析快照(MarketAnalytics),
回调通知策略引擎。
"""

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from at01_common.settings import get_settings
from at01_common.logger import LoggerMixin
from at30_analytics.accumulation import AccumulationDetector, AccumulationResult
from at30_analytics.indicators import CVDTracker, DeltaTracker, VWAPCalculator
from at30_analytics.whale import WhaleDetector, WhaleEvent
from at20_market.market_models import TradeTick

AnalyticsCallback = Callable[[str, "MarketAnalytics"], Awaitable[None]]


@dataclass
class MarketAnalytics:
    """单个标的的市场分析快照"""

    symbol: str
    price: float = 0.0
    ts: int = 0  # ms

    # 量价
    vwap: float = 0.0
    vwap_deviation: float = 0.0  # 相对 VWAP 偏离率
    vwap_upper: float = 0.0
    vwap_lower: float = 0.0

    # 订单流
    delta: float = 0.0
    delta_ratio: float = 0.0
    cvd: float = 0.0
    cvd_rising: bool = False
    cvd_falling: bool = False
    cvd_slope: float = 0.0

    # 大单
    last_whale: Optional[dict[str, Any]] = None
    whale_buy_count_recent: int = 0
    whale_sell_count_recent: int = 0

    # 吸筹
    accumulation: float = 0.0  # 0~1 分数
    is_accumulating: bool = False
    accumulation_reasons: list[str] = field(default_factory=list)

    # 趋势(供策略用)
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    trend: str = "neutral"  # up / down / neutral

    # V2.0: 价格区间 / 量比 / 市场环境
    recent_high: float = 0.0  # 近期高点(评分用价格位置)
    recent_low: float = 0.0
    volume_ratio: float = 1.0  # 近期量/基期量
    regime: str = ""  # BULL/SIDEWAY/BEAR/PANIC(Market Regime Engine 注入)
    change_pct_24h: float = 0.0  # V3.0: 24h 涨跌幅(情绪因子)

    # V2.0: 订单流
    buy_pressure: float = 0.0  # 主动买入额(窗口)
    sell_pressure: float = 0.0  # 主动卖出额(窗口)
    buy_sell_ratio: float = 0.0  # 买压/卖压
    large_order_ratio: float = 0.0  # 大单成交额占比

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "ts": self.ts,
            "vwap": self.vwap,
            "vwap_deviation": self.vwap_deviation,
            "vwap_upper": self.vwap_upper,
            "vwap_lower": self.vwap_lower,
            "delta": self.delta,
            "delta_ratio": self.delta_ratio,
            "cvd": self.cvd,
            "cvd_rising": self.cvd_rising,
            "cvd_falling": self.cvd_falling,
            "cvd_slope": self.cvd_slope,
            "last_whale": self.last_whale,
            "whale_buy_count_recent": self.whale_buy_count_recent,
            "whale_sell_count_recent": self.whale_sell_count_recent,
            "accumulation": self.accumulation,
            "is_accumulating": self.is_accumulating,
            "accumulation_reasons": self.accumulation_reasons,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "trend": self.trend,
            "recent_high": self.recent_high,
            "recent_low": self.recent_low,
            "volume_ratio": self.volume_ratio,
            "regime": self.regime,
            "change_pct_24h": self.change_pct_24h,
            "buy_pressure": self.buy_pressure,
            "sell_pressure": self.sell_pressure,
            "buy_sell_ratio": self.buy_sell_ratio,
            "large_order_ratio": self.large_order_ratio,
        }


class _SymbolAnalytics:
    """单标的分析器集合"""

    def __init__(self):
        s = get_settings()
        self.vwap = VWAPCalculator(window=s.analytics_vwap_window)
        self.delta = DeltaTracker(window=s.analytics_vwap_window)
        self.cvd = CVDTracker()
        self.whale = WhaleDetector(
            min_quote=s.analytics_whale_min_quote,
            quantile=s.analytics_whale_quantile,
        )
        self.accumulation = AccumulationDetector(
            window_seconds=s.analytics_accumulation_window,
            whale_threshold=s.analytics_whale_min_quote,
        )
        self.ema_fast: Optional[float] = None
        self.ema_slow: Optional[float] = None
        self.whale_events: deque[WhaleEvent] = deque(maxlen=100)
        # V2.0: 价格区间 / 量比 / 订单流
        self.recent_high: float = 0.0
        self.recent_low: float = 0.0
        self.volume_window: deque[float] = deque(maxlen=60)  # 每秒成交额
        self.volume_base: deque[float] = deque(maxlen=600)  # 基期(10分钟)
        self.last_volume_bucket: int = -1
        self.buy_pressure: float = 0.0
        self.sell_pressure: float = 0.0
        self.large_order_quote: float = 0.0
        self.total_quote: float = 0.0

    def _update_volume(self, tick: TradeTick) -> None:
        """按秒聚合成交额,计算量比"""
        bucket = tick.trade_time // 1000
        if bucket != self.last_volume_bucket:
            if self.last_volume_bucket >= 0 and self.volume_window:
                self.volume_base.append(self.volume_window[-1])
            self.volume_window.append(0.0)
            self.last_volume_bucket = bucket
        if self.volume_window:
            self.volume_window[-1] += tick.quote_quantity

    @property
    def volume_ratio(self) -> float:
        """近期量比: 最近窗口均值 / 基期均值"""
        if not self.volume_base:
            return 1.0
        base_avg = sum(self.volume_base) / len(self.volume_base)
        if base_avg <= 0 or not self.volume_window:
            return 1.0
        recent_avg = sum(self.volume_window) / len(self.volume_window)
        return recent_avg / base_avg

    @staticmethod
    def _ema(prev: Optional[float], value: float, period: int) -> float:
        """指数移动平均"""
        if prev is None:
            return value
        k = 2 / (period + 1)
        return value * k + prev * (1 - k)

    def update_ema(self, price: float, fast_period: int, slow_period: int) -> None:
        """按最新价更新 EMA(以成交笔数近似周期)"""
        self.ema_fast = self._ema(self.ema_fast, price, fast_period)
        self.ema_slow = self._ema(self.ema_slow, price, slow_period)


class AnalyticsEngine(LoggerMixin):
    """分析引擎"""

    def __init__(
        self,
        symbols: Optional[list[str]] = None,
        on_analytics: Optional[AnalyticsCallback] = None,
    ):
        self.settings = get_settings()
        self.symbols = symbols or self.settings.symbol_list
        self.on_analytics = on_analytics

        self._analyzers: dict[str, _SymbolAnalytics] = defaultdict(_SymbolAnalytics)
        self._latest: dict[str, MarketAnalytics] = {}
        self._whale_events: deque[WhaleEvent] = deque(maxlen=200)  # 全局事件流
        self._regimes: dict[str, str] = {}  # Market Regime Engine 注入(V2.0)
        self._change_24h: dict[str, float] = {}  # V3.0: 24h 涨跌幅

    def set_regime(self, symbol: str, regime: str) -> None:
        """注入市场环境(供策略评分参考)"""
        self._regimes[symbol] = regime

    def set_change_24h(self, symbol: str, change_pct: float) -> None:
        """注入 24h 涨跌幅(情绪因子)"""
        self._change_24h[symbol] = change_pct

    # ---------- 主入口 ----------

    async def on_trade(self, symbol: str, tick: TradeTick) -> MarketAnalytics:
        """行情引擎成交回调:更新所有指标,产出快照"""
        az = self._analyzers[symbol]

        vwap_res = az.vwap.update(tick.price, tick.quantity)
        if vwap_res.vwap > 0:
            vwap_res.deviation = (tick.price - vwap_res.vwap) / vwap_res.vwap

        delta_res = az.delta.update(tick)
        cvd_res = az.cvd.update(tick)

        whale_evt = az.whale.update(tick)
        if whale_evt:
            az.whale_events.append(whale_evt)
            self._whale_events.append(whale_evt)
            self.logger.info(
                "大单",
                symbol=symbol,
                side=whale_evt.side,
                quote=round(whale_evt.quote_quantity, 2),
                price=whale_evt.price,
            )

        acc_res = az.accumulation.update(tick)

        az.update_ema(
            tick.price,
            fast_period=self.settings.trend_fast_period,
            slow_period=self.settings.trend_slow_period,
        )

        # V2.0: 价格区间 / 量比 / 订单流统计
        az.recent_high = max(az.recent_high, tick.price)
        if az.recent_low <= 0:
            az.recent_low = tick.price
        else:
            az.recent_low = min(az.recent_low, tick.price)
        # 区间衰减(跟随市场移动,窗口约 5 分钟)
        if az.recent_high > 0 and tick.price < az.recent_high * 0.995:
            az.recent_high *= 0.99995
        if az.recent_low > 0 and tick.price > az.recent_low * 1.005:
            az.recent_low = min(az.recent_low * 1.00005, tick.price)

        az._update_volume(tick)
        az.total_quote += tick.quote_quantity
        if tick.is_buyer_maker:
            az.sell_pressure += tick.quote_quantity
        else:
            az.buy_pressure += tick.quote_quantity
        if tick.quote_quantity >= self.settings.analytics_whale_min_quote:
            az.large_order_quote += tick.quote_quantity
        # 环形重置(每 5 分钟清零,保持"近期"语义)
        if az.total_quote > 5_000_000:
            az.buy_pressure *= 0.5
            az.sell_pressure *= 0.5
            az.large_order_quote *= 0.5
            az.total_quote *= 0.5

        # 趋势判定
        if az.ema_fast and az.ema_slow:
            spread = (az.ema_fast - az.ema_slow) / az.ema_slow
            trend = "up" if spread > 0.0002 else ("down" if spread < -0.0002 else "neutral")
        else:
            trend = "neutral"

        snapshot = MarketAnalytics(
            symbol=symbol,
            price=tick.price,
            ts=tick.trade_time,
            vwap=vwap_res.vwap,
            vwap_deviation=vwap_res.deviation,
            vwap_upper=vwap_res.upper_band,
            vwap_lower=vwap_res.lower_band,
            delta=delta_res.delta,
            delta_ratio=delta_res.delta_ratio,
            cvd=cvd_res.cvd,
            cvd_rising=cvd_res.rising,
            cvd_falling=cvd_res.falling,
            cvd_slope=cvd_res.cvd_slope,
            last_whale=(
                {
                    "side": whale_evt.side,
                    "price": whale_evt.price,
                    "quote": whale_evt.quote_quantity,
                    "ts": whale_evt.trade_time,
                }
                if whale_evt
                else None
            ),
            whale_buy_count_recent=sum(1 for e in az.whale_events if e.side == "buy"),
            whale_sell_count_recent=sum(1 for e in az.whale_events if e.side == "sell"),
            accumulation=acc_res.score,
            is_accumulating=acc_res.is_accumulating,
            accumulation_reasons=acc_res.reasons,
            ema_fast=az.ema_fast or 0.0,
            ema_slow=az.ema_slow or 0.0,
            trend=trend,
            # V2.0
            recent_high=az.recent_high,
            recent_low=az.recent_low,
            volume_ratio=round(az.volume_ratio, 4),
            regime=self._regimes.get(symbol, ""),
            change_pct_24h=self._change_24h.get(symbol, 0.0),
            buy_pressure=round(az.buy_pressure, 2),
            sell_pressure=round(az.sell_pressure, 2),
            buy_sell_ratio=(
                round(az.buy_pressure / az.sell_pressure, 4) if az.sell_pressure > 0 else 0.0
            ),
            large_order_ratio=(
                round(az.large_order_quote / az.total_quote, 4) if az.total_quote > 0 else 0.0
            ),
        )
        self._latest[symbol] = snapshot

        if self.on_analytics:
            await self.on_analytics(symbol, snapshot)
        return snapshot

    # ---------- 对外查询 ----------

    def get(self, symbol: str) -> Optional[MarketAnalytics]:
        """最新分析快照"""
        return self._latest.get(symbol)

    def get_all(self) -> dict[str, MarketAnalytics]:
        """全部最新快照"""
        return dict(self._latest)

    def recent_whales(self, limit: int = 50) -> list[dict[str, Any]]:
        """近期大单事件"""
        return [
            {
                "symbol": e.symbol,
                "side": e.side,
                "price": e.price,
                "quantity": e.quantity,
                "quote": e.quote_quantity,
                "threshold": e.threshold,
                "ts": e.trade_time,
            }
            for e in list(self._whale_events)[-limit:]
        ]

    def snapshot(self) -> dict[str, Any]:
        """全量快照(Web 用)"""
        return {
            "ts": int(time.time() * 1000),
            "symbols": {s: a.to_dict() for s, a in self._latest.items()},
            "whales": self.recent_whales(30),
        }
