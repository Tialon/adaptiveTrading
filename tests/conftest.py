"""
pytest 全局夹具

- 注入各模块目录到 sys.path(目录带数字前缀)
- 提供独立 settings 单例清理
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
for d in ("00_common", "10_web", "20_market", "30_ayalytics", "50_startegy", "50_execution", "60_risk"):
    p = str(ROOT / d)
    if p not in sys.path:
        sys.path.insert(0, p)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每个测试前清理 settings 缓存,保证默认配置"""
    from common.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()


@pytest.fixture
def trade_tick_factory():
    """构造 TradeTick 的工厂"""
    from market.models import TradeTick

    counter = {"id": 0}

    def make(
        price: float = 100.0,
        quantity: float = 1.0,
        is_buyer_maker: bool = False,
        symbol: str = "BTCUSDT",
        ts: int | None = None,
    ):
        counter["id"] += 1
        return TradeTick(
            trade_id=counter["id"],
            symbol=symbol,
            price=price,
            quantity=quantity,
            quote_quantity=price * quantity,
            is_buyer_maker=is_buyer_maker,
            trade_time=ts if ts is not None else 1_700_000_000_000 + counter["id"] * 1000,
        )

    return make
