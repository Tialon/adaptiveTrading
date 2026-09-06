"""
Timeframe 工具(V7)

统一时间粒度换算, 全系统唯一来源(Sharpe 年化/分页数据量/attribution):
    interval_to_seconds / bars_per_day / bars_per_year
"""

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400,
}


def interval_to_seconds(interval: str) -> int:
    """interval -> 秒(未知按 1m)"""
    return INTERVAL_SECONDS.get(interval, 60)


def bars_per_day(interval: str) -> int:
    """每天 bar 数"""
    return 86400 // interval_to_seconds(interval)


def bars_per_year(interval: str) -> int:
    """每年 bar 数(Sharpe 年化用)"""
    return bars_per_day(interval) * 365
