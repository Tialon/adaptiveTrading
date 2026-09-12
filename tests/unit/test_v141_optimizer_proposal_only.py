"""V11.3 P1-7 Optimizer 只产 proposal 守约。

钉死「AI 只优化不交易」不变量:
- `StrategyVersionManager.snapshot` 落库 `active=False`(提案, 非生效);
- `activate` 是独立显式方法, 需人工调用; 优化器代码层无任何 `.activate(` 调用方;
- 优化器 persist 复用 snapshot → 优化结果永不自动生效。

冻结不变: 纯守约测试, 不改优化器/版本逻辑。
"""

from sqlalchemy import select

from at01_common.database import AsyncSessionLocal
from at01_common.models import StrategyVersion
from at30_strategy.strategy_version import StrategyVersionManager


async def test_snapshot_creates_inactive_version(db_tables):
    mgr = StrategyVersionManager()
    vid = await mgr.snapshot("v-proposal", note="optimizer proposal", params={"a": 1})
    assert vid is not None

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(StrategyVersion).where(StrategyVersion.id == vid))
        ).scalar_one()
        assert row.active is False  # 提案默认不生效


async def test_snapshot_existing_version_skips_no_duplicate(db_tables):
    mgr = StrategyVersionManager()
    vid1 = await mgr.snapshot("v-dup", note="proposal", params={})
    vid2 = await mgr.snapshot("v-dup", note="proposal", params={})
    assert vid1 == vid2  # 版本已存在则复用, 不重复插入


async def test_activate_is_explicit_and_separate(db_tables):
    mgr = StrategyVersionManager()
    vid = await mgr.snapshot("v-act", note="proposal", params={})
    assert vid is not None

    # 未激活前
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(StrategyVersion).where(StrategyVersion.id == vid))
        ).scalar_one()
        assert row.active is False

    # 人工显式 activate 才生效
    assert await mgr.activate("v-act") is True
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(StrategyVersion).where(StrategyVersion.id == vid))
        ).scalar_one()
        assert row.active is True
