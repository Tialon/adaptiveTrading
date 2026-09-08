"""
pytest 全局夹具

- 注入各模块目录到 sys.path(目录带数字前缀)
- 提供独立 settings 单例清理
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# 测试环境: 固定关键配置,屏蔽 .env 差异(冻结单币 SOLUSDT)
os.environ["SYMBOLS"] = "SOLUSDT"
os.environ["PAPER_TRADING"] = "true"
os.environ["AI_ENABLED"] = "false"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["REDIS_ENABLED"] = "false"

for d in ("at01_common", "at10_web", "at20_market", "at30_analytics", "at40_journal", "at50_strategy", "at50_execution", "at55_portfolio", "at60_risk", "at70_backtest", "at80_optimizer"):
    p = str(ROOT / d)
    if p not in sys.path:
        sys.path.insert(0, p)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch):
    """每个测试前清理 settings/引擎 缓存,保证测试环境隔离"""
    import at01_common.database as db_mod
    from at01_common import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    db_mod.reset_engine()
    yield
    settings_mod.get_settings.cache_clear()
    db_mod.reset_engine()


@pytest.fixture
async def db_tables():
    """建表 fixture(需要数据库的测试用)"""
    import at01_common.database as db_mod

    await db_mod.init_db()
    yield


@pytest.fixture
def trade_tick_factory():
    """构造 TradeTick 的工厂"""
    from at20_market.market_models import TradeTick

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
