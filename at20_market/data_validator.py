"""
行情数据校验器(V8)

检测数据源异常, 供上层(RiskManager.pause)暂停交易:
1. K线时间缺口: 相邻收盘 bar 间隔远超单周期(WS 断流/数据丢失)
2. 价格跳变: 相邻收盘价单根跳变超阈值(喂价异常/闪崩毛刺)
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


class MarketDataValidator(LoggerMixin):
    """行情数据完整性校验"""

    def __init__(self, max_price_jump_pct: float = 0.15, max_gap_intervals: int = 3):
        self.max_price_jump_pct = max_price_jump_pct
        self.max_gap_intervals = max_gap_intervals

    def check_gap(
        self, prev_open_time: Optional[int], open_time: int, interval: str
    ) -> Optional[int]:
        """返回缺口毫秒数(超过允许间隔), 否则 None"""
        ms = INTERVAL_MS.get(interval)
        if not ms or prev_open_time is None:
            return None
        gap = open_time - prev_open_time
        if gap > ms * self.max_gap_intervals:
            return gap
        return None

    def check_price_jump(self, prev_close: Optional[float], close: float) -> bool:
        if prev_close is None or prev_close <= 0:
            return False
        return abs(close - prev_close) / prev_close >= self.max_price_jump_pct

    def validate_closed_bar(self, prev: Any, bar: Any) -> list[str]:
        """校验一根收盘 bar(prev 为上一根收盘 bar 或 None), 返回异常描述列表"""
        issues: list[str] = []
        if prev is not None:
            gap = self.check_gap(prev.open_time, bar.open_time, bar.interval)
            if gap:
                issues.append(f"K线缺口 {gap // 1000}s")
            if self.check_price_jump(prev.close, bar.close):
                issues.append(f"价格跳变 {prev.close:.4f}->{bar.close:.4f}")
        return issues
