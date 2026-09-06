"""
时间工具
"""

import time
from datetime import datetime, timezone


def now_ms() -> int:
    """当前毫秒时间戳"""
    return int(time.time() * 1000)


def ms_to_datetime(ms: int) -> datetime:
    """毫秒时间戳转 UTC datetime"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def datetime_to_ms(dt: datetime) -> int:
    """datetime 转毫秒时间戳"""
    return int(dt.timestamp() * 1000)
