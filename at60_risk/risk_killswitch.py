"""
急停开关(V10)

区别于 CircuitBreaker(带 cooldown 自动复位): 急停冻结一旦触发, 必须人工
POST /api/emergency/recover 才解除。持久化到 kill_switch_state 表(单行 id=1),
重启后仍保持冻结状态, 承载:
1. 启动对账未通过 -> 冻结;
2. 周期对账权益严重漂移 -> 冻结;
3. 人工急停 -> 冻结。
"""

from typing import Any

from at01_common.logger import LoggerMixin


class KillSwitch(LoggerMixin):
    """持久化急停开关(不自动复位)"""

    def __init__(self):
        self._armed: bool = False
        self._reason: str = ""

    # ---------- 状态 ----------

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def reason(self) -> str:
        return self._reason

    # ---------- 操作 ----------

    def arm(self, reason: str) -> None:
        """触发急停(幂等, 首次触发告警)"""
        if not self._armed:
            self.logger.error("急停开关触发", reason=reason)
        self._armed = True
        self._reason = reason

    def disarm(self) -> None:
        """人工解除"""
        self._armed = False
        self._reason = ""
        self.logger.info("急停开关已解除")

    # ---------- 持久化 ----------

    async def load_from_db(self) -> None:
        """启动时加载急停状态(单行 id=1)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import KillSwitchState

        try:
            async with AsyncSessionLocal() as session:
                row = (await session.execute(select(KillSwitchState))).scalars().first()
                if row is not None:
                    self._armed = row.armed
                    self._reason = row.reason
                    self.logger.info("急停状态已加载", armed=self._armed, reason=self._reason)
        except Exception:
            self.logger.exception("急停状态加载失败")

    async def persist(self) -> None:
        """持久化急停状态(单行 upsert)"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import KillSwitchState

        try:
            async with AsyncSessionLocal() as session:
                row = (await session.execute(select(KillSwitchState))).scalars().first()
                if row is None:
                    row = KillSwitchState(armed=self._armed, reason=self._reason)
                    session.add(row)
                else:
                    row.armed = self._armed
                    row.reason = self._reason
                await session.commit()
        except Exception:
            self.logger.exception("急停状态持久化失败")

    def status(self) -> dict[str, Any]:
        return {"armed": self._armed, "reason": self._reason}
