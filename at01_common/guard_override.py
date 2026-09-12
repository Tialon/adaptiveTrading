"""启动守卫显式解锁(V12.6 P2)

**这个模块解决什么**: 切换运行模式原本要逐项修配置 → 重启 → 读日志 → 再修, 摩擦很大。
本模块提供一个**显式、留痕、会过期**的一次性解锁通道, 把「改 8 个配置文件」变成
「填一次确认短语」。

**它解锁什么、不解锁什么** —— 这条边界是整个设计的关键:

| | 解锁? | 说明 |
|---|---|---|
| 启动前守卫(`mainnet_blocked_reason` / `mainnet_readiness_check` 九项 / `testnet_gate`) | ✅ **解锁** | 一次性预检, 防的是「配错一个变量就连上主网」 |
| 运行时闸门(`TradingGate` 六维+两维) | ❌ **不解锁** | **每一笔单**的判定, 防的是行情静默 / 账本漂移 / 资金熔断 / 关键任务崩溃 |

拆掉后者不是「提高易用性」, 是让系统拿着错误的账本去下真钱单 ——
Pi 上的 `equity_drift` 假阳性就是例证(已由 `live_equity.py` 修根因)。

**为什么带到期时间**: 一个「永久关闭」的开关迟早会被遗忘成默认状态。
到期即自动失效, 强制重新确认。

格式: `GUARD_OVERRIDE=<ISO8601 到期时间>:<确认短语>`
例:   `GUARD_OVERRIDE=2026-09-13T00:00:00Z:I-KNOW-THIS-IS-MAINNET`
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

# 必须逐字匹配的确认短语(大小写敏感, 防手滑)
CONFIRM_PHRASE = "I-KNOW-THIS-IS-MAINNET"

# 生效时的常驻告警文案(启动日志 / 页面横幅共用, 避免两处措辞漂移)
BANNER = (
    "启动守卫已被显式解锁(GUARD_OVERRIDE 生效) —— 主网/测试网预检本次**跳过**。"
    "注意: 运行时交易闸门(TradingGate)不受影响, 仍逐笔判定买卖许可。"
)


@dataclass(frozen=True)
class GuardOverride:
    """解锁状态。`active=True` 才放行; 任何解析失败都 fail-closed(active=False)。"""

    active: bool
    expires_at: float = 0.0
    reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "expires_at": self.expires_at,
            "expires_at_iso": (
                datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat()
                if self.expires_at else ""
            ),
            "reason": self.reason,
            "error": self.error,
            "banner": BANNER if self.active else "",
        }


def parse_guard_override(raw: str, *, now: float | None = None) -> GuardOverride:
    """解析 `GUARD_OVERRIDE`。

    fail-closed: 空值 / 格式错 / 短语不匹配 / 已过期 / 时间非法 —— **一律 active=False**。
    只有「格式正确 + 短语逐字匹配 + 尚未过期」三者同时成立才放行。
    """
    now = time.time() if now is None else now
    raw = (raw or "").strip()
    if not raw:
        return GuardOverride(active=False)

    # 短语不含冒号, 故从**最后一个**冒号切分, 兼容 ISO8601 里的时:分:秒
    if ":" not in raw:
        return GuardOverride(active=False, error="格式应为 <ISO8601 到期时间>:<确认短语>")
    ts_part, _, phrase = raw.rpartition(":")
    phrase = phrase.strip()
    if phrase != CONFIRM_PHRASE:
        return GuardOverride(active=False, error="确认短语不匹配")

    try:
        expires_at = datetime.fromisoformat(ts_part.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return GuardOverride(active=False, error=f"到期时间无法解析: {ts_part!r}")

    if expires_at <= now:
        return GuardOverride(
            active=False,
            expires_at=expires_at,
            error="已过期 —— 请更新到期时间后重启(过期即自动失效, 不保留任何永久解锁)",
        )

    return GuardOverride(
        active=True,
        expires_at=expires_at,
        reason=f"操作者显式解锁, 有效至 {datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()}",
    )
