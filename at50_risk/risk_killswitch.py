"""
急停开关(V10)

区别于 CircuitBreaker(带 cooldown 自动复位): 急停冻结一旦触发, 需解除才恢复交易。
持久化到 kill_switch_state 表(单行 id=1), 重启后仍保持冻结状态, 承载:
1. 启动对账未通过 -> 冻结;
2. 周期对账权益严重漂移 -> 冻结;
3. 人工急停 -> 冻结;
4. 关键后台任务异常退出 -> 冻结。

**V13: `origin` —— 区分「谁冻的」**。此前所有来源共用一个「必须人工解除」的语义,
于是**关键后台任务崩溃这种系统自己造成、且重启任务即可恢复**的冻结, 也必须等人。
V13 把来源记下来: 只有 `SELF_HEALABLE_KILL_ORIGINS`(对账瞬态/数据/任务)才允许
`at50_risk/auto_recovery.py` 自动解除; 人工冻结与重大资金异常仍走人工确认。

⚠️ `origin` **默认 MANUAL** 是刻意的 fail-closed: 老库补列后既有行、以及任何忘记
传来源的新调用点, 一律按「需要人」处理, 不会因为升级或漏改而突然获得自动解冻能力。
"""

from typing import Any

from at01_common.logger import LoggerMixin
from at01_common.operator_narrative import KILL_ORIGIN_MANUAL


class KillSwitch(LoggerMixin):
    """持久化急停开关(不自动复位)"""

    def __init__(self):
        self._armed: bool = False
        self._reason: str = ""
        self._origin: str = KILL_ORIGIN_MANUAL

    # ---------- 状态 ----------

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def origin(self) -> str:
        """冻结来源(见 `at01_common/operator_narrative.py` 的 KILL_ORIGIN_*)。"""
        return self._origin

    # ---------- 操作 ----------

    def arm(self, reason: str, origin: str = KILL_ORIGIN_MANUAL) -> None:
        """触发急停(幂等, 首次触发告警)。

        `origin` 缺省 MANUAL —— 不传就是「需要人」, 见模块 docstring。
        """
        if not self._armed:
            self.logger.error("急停开关触发", reason=reason, origin=origin)
        self._armed = True
        self._reason = reason
        self._origin = origin or KILL_ORIGIN_MANUAL

    def disarm(self) -> None:
        """解除冻结(人工确认, 或由 `auto_recovery` 在条件全部复核通过后调用)。"""
        self._armed = False
        self._reason = ""
        self._origin = KILL_ORIGIN_MANUAL
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
                    # 老行/缺失值按 MANUAL 兜底(fail-closed, 不因缺字段而获得自动解冻能力)
                    self._origin = row.origin or KILL_ORIGIN_MANUAL
                    self.logger.info(
                        "急停状态已加载", armed=self._armed, reason=self._reason,
                        origin=self._origin,
                    )
        except Exception:
            self.logger.exception("急停状态加载失败")

    async def persist(self) -> bool:
        """持久化急停状态(单行 upsert, 带重试)。

        急停冻结关系资金安全, 持久化失败不得静默吞掉 —— 失败重试若干次后仍失败
        返回 False 并告警(调用方据此告警/进入恢复核验)。返回 True 表示落库成功。
        """
        import asyncio

        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import KillSwitchState

        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with AsyncSessionLocal() as session:
                    row = (await session.execute(select(KillSwitchState))).scalars().first()
                    if row is None:
                        row = KillSwitchState(
                            armed=self._armed, reason=self._reason, origin=self._origin
                        )
                        session.add(row)
                    else:
                        row.armed = self._armed
                        row.reason = self._reason
                        row.origin = self._origin
                    await session.commit()
                return True
            except Exception as exc:
                last_err = exc
                self.logger.warning(
                    "急停状态持久化失败, 重试", attempt=attempt, error=str(exc),
                )
                await asyncio.sleep(0.1 * attempt)
        self.logger.error(
            "急停状态持久化失败(重试后仍失败, 重启后可能丢失急停态)",
            error=str(last_err),
        )
        return False

    def status(self) -> dict[str, Any]:
        return {"armed": self._armed, "reason": self._reason, "origin": self._origin}
