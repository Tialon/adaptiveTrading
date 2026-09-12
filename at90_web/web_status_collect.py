"""操作者状态的**带 I/O 收集层**(V13 W3)。

`web_operator_status.build_operator_status()` 刻意保持**纯函数** —— 这正是它能被穷举
测试的原因(7×10×5 种状态组合逐条断言)。但首屏要显示的几件事必须真的去问:

    「数据库还连着吗?」「磁盘还剩多少?」「今天成交了几笔?」「这次急停是谁触发的?」

把 I/O 放在这里, 纯函数放在那里, 两边各自单纯。路由层只调本模块。

**失败一律降级, 绝不抛**: 任何一个探测失败都不该让 `/api/operator-status` 变成 500 ——
那样用户看到的是白屏, 比看到「某项未探测」糟糕得多。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from at01_common.logger import get_logger

logger = get_logger("WebStatusCollect")


# V14: 依赖不可用时的探测/统计超时。
#
# 实测踩到过: MySQL 停掉之后, `/api/operator-status` **挂住 20 秒**才返回 —— 因为底层
# 连接尝试要等操作系统级超时。用户看到的是页面转圈, 而不是「数据库不可达」。
# 首屏接口必须**快速失败并如实说话**, 不能把「等待」当成回答。
DB_PROBE_TIMEOUT = 3.0


async def _with_timeout(coro: Any, timeout: float, what: str) -> Any:
    """给一次依赖调用加超时; 超时/失败一律返回 None(调用方按「不可用」处理)。"""
    import asyncio

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("依赖探测超时", what=what, timeout=timeout)
        return None
    except Exception as exc:
        logger.warning("依赖探测失败", what=what, error=str(exc))
        return None


async def probe_db() -> bool | None:
    """数据库连通性探测。返回 None 表示**没能探测**(而不是「探测失败」)。

    区分这两者是刻意的: 未探测时应显示「未单独探测」, 不能冒充「正常」。
    带超时 —— 见 `DB_PROBE_TIMEOUT` 的说明。
    """
    import asyncio

    async def _ping() -> bool:
        from sqlalchemy import text

        from at01_common.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True

    try:
        return await asyncio.wait_for(_ping(), timeout=DB_PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("数据库连通性探测超时", timeout=DB_PROBE_TIMEOUT)
        return False
    except Exception as exc:
        logger.warning("数据库连通性探测失败", error=str(exc))
        return False


async def collect_today_stats(state: Any, *, skip_db: bool = False) -> dict[str, Any]:
    """今日交易统计(成交笔数/盈亏/胜率/最大回撤) + 来自事件流的稳定性计数。

    数据源:
    - 交易笔数与盈亏 —— `trade_records`(`ClosedTrade`, 平仓周期) 今日行;
    - 胜率 —— 其中 `realized_pnl > 0` 的占比;
    - 最大回撤 —— 今日 `risk_events` 中 `drawdown_tier` 的最大值(没有就 0);
    - 自动恢复 / WS 重连 / 人工干预 —— `operator_log.day_summary()`。

    任一环节失败只让对应字段缺失, 不影响其余。

    `skip_db=True`(由调用方在**已知库不可达**时传)会跳过全部库读取 —— 否则每个查询
    都要各自等一次超时, 首屏会慢成「数据库不可达」的四倍(实测 12 秒)。
    """
    out: dict[str, Any] = {}
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since_ms = int(today_start.timestamp() * 1000)

    async def _read_trades() -> list[Any]:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import ClosedTrade

        async with AsyncSessionLocal() as session:
            return list((await session.execute(select(ClosedTrade))).scalars().all())

    rows = [] if skip_db else (
        await _with_timeout(_read_trades(), DB_PROBE_TIMEOUT, "今日成交") or []
    )
    try:
        todays = [r for r in rows if int(getattr(r, "exit_ts", 0) or 0) >= since_ms]
        pnl = sum(float(getattr(r, "realized_pnl", 0.0) or 0.0) for r in todays)
        wins = sum(1 for r in todays if float(getattr(r, "realized_pnl", 0.0) or 0.0) > 0)
        out["trades"] = len(todays)
        out["pnl"] = round(pnl, 2)
        out["win_rate"] = (wins / len(todays)) if todays else 0.0
        cost = sum(
            float(getattr(r, "entry_price", 0.0) or 0.0)
            * float(getattr(r, "quantity", 0.0) or 0.0)
            for r in todays
        )
        out["pnl_pct"] = round(pnl / cost * 100, 4) if cost > 0 else 0.0
    except Exception as exc:
        logger.warning("今日交易统计读取失败", error=str(exc))

    from at01_common.operator_events import operator_log

    summary = None if skip_db else await _with_timeout(
        operator_log.day_summary(), DB_PROBE_TIMEOUT, "今日稳定性"
    )
    if summary:
        out["auto_recoveries"] = summary.get("auto_recoveries", 0)
        out["ws_reconnects"] = summary.get("ws_reconnects", 0)
        out["human_interventions"] = summary.get("human_interventions", 0)

    async def _read_drawdowns() -> list[Any]:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import RiskEvent

        async with AsyncSessionLocal() as session:
            return list(
                (
                    await session.execute(
                        select(RiskEvent).where(RiskEvent.event_type == "drawdown")
                    )
                )
                .scalars()
                .all()
            )

    dd_rows = [] if skip_db else (
        await _with_timeout(_read_drawdowns(), DB_PROBE_TIMEOUT, "今日回撤") or []
    )
    peak = 0.0
    for r in dd_rows:
        ts = getattr(r, "created_at", None)
        if ts is not None and ts.timestamp() * 1000 < since_ms:
            continue
        peak = max(peak, _extract_pct(getattr(r, "detail", "") or ""))
    out["max_drawdown_pct"] = round(peak, 4)

    return out


def _extract_pct(detail: str) -> float:
    """从 `risk_events.detail` 的自由文本里取回撤百分比(形如「回撤3.20% 触发急停」)。"""
    import re

    m = re.search(r"(\d+(?:\.\d+)?)\s*%", detail)
    return float(m.group(1)) if m else 0.0


async def collect_extras(state: Any) -> dict[str, Any]:
    """收集 `build_operator_status(extras=...)` 需要的全部事实。"""
    from at90_web.web_health_report import probe_disk

    db_ok = await probe_db()
    extras: dict[str, Any] = {
        "db_ok": db_ok,
        "disk": probe_disk("."),
        # 库不可达时不再逐项去读 —— 见 collect_today_stats 的 skip_db 说明
        "today": await collect_today_stats(state, skip_db=db_ok is False),
        "persist_failures": 0,
        "kill_origin": "",
    }
    try:
        from at01_common.operator_events import operator_log

        extras["persist_failures"] = operator_log.status().get("persist_failures", 0)
    except Exception:
        pass
    # 急停来源 —— 决定结论卡说「等系统自愈」还是「需要你的确认」
    try:
        rm = getattr(state, "risk_manager", None)
        if rm is not None:
            extras["kill_origin"] = str(getattr(rm.kill_switch, "origin", "") or "")
            # 账户权益(首屏四项之一)。取不到就留空 —— 页面显示「--」而不是编一个 0。
            market = getattr(state, "market_engine", None)
            last_prices = {
                s: st.last_price for s, st in (getattr(market, "state", {}) or {}).items()
            }
            extras["equity"] = round(rm.equity(last_prices), 2)
    except Exception as exc:
        logger.warning("账户权益读取失败", error=str(exc))
    return extras


def now_ts() -> float:
    """当前时间戳(供路由加在响应里, 便于前端判断数据新鲜度)。"""
    return time.time()
