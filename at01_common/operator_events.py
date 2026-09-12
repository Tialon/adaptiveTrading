"""操作员事件流(V13 P0) —— 「今天发生了什么」的人话日志。

**这个模块解决什么**: 此前想知道系统今天经历了什么, 只能 `docker compose logs | grep`。
技术日志服务的是「为什么发生」(开发者), 而操作者要的是「发生了什么」。两者必须共存,
所以本模块**不替换** logger, 而是在关键节点额外记一条**已经翻译好的人话**:

    14:32:01 系统启动
    14:32:03 Binance 连接成功
    14:32:05 账户同步完成
    14:32:07 对账完成
    14:32:10 策略进入 READY
    14:35:21 发现 BUY 信号 → 风控通过 → 订单提交 → 成交 → 持仓更新 → 交易完成

**两条硬约束(决定了这里的实现形态)**:

1. **绝不因记日志失败而影响交易**。`emit()` 是**同步**的: 先写内存环(不可能失败),
   再 best-effort 落库; 落库异常只记账不抛出。因此可以在同步回调(如
   `RuntimeSupervisor` 的 critical-failure 回调)与异常路径里安全调用。
2. **脱敏**。`detail` 里塞进来的技术载荷会经 `config_store.is_sensitive_key` + 文本扫描
   二次过滤, 含 KEY/SECRET/TOKEN/PASSWORD 的键与值一律掩码 —— 事件流会进日报与
   AI Review, 不能成为密钥泄露通道。

**内存环 vs 落库**: 页面 WS 推送要秒级(读内存环, 同步、无 I/O); 「今日系统复盘」与
重启后的回溯要持久(读库)。两者都在, 各自服务不同场景。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

from at01_common.logger import get_logger

logger = get_logger("OperatorEvents")

# ---------------------------------------------------------------------------
# 事件类型(单一来源, 供埋点/页面/测试引用)
# ---------------------------------------------------------------------------

KIND_STARTUP = "STARTUP"
KIND_CONNECT = "CONNECT"
KIND_ACCOUNT_SYNC = "ACCOUNT_SYNC"
KIND_RECONCILE = "RECONCILE"
KIND_READY = "READY"
KIND_SIGNAL = "SIGNAL"
KIND_RISK_PASS = "RISK_PASS"
KIND_RISK_BLOCK = "RISK_BLOCK"   # 风控拦下了某个信号 —— 正常的预期结果, 不是故障
KIND_ORDER_SUBMIT = "ORDER_SUBMIT"
KIND_FILL = "FILL"
KIND_POSITION = "POSITION"
KIND_TRADE_DONE = "TRADE_DONE"
KIND_RECONNECT = "RECONNECT"
KIND_RECOVERY = "RECOVERY"
KIND_DEGRADE = "DEGRADE"
KIND_KILL = "KILL"
KIND_RECOVER = "RECOVER"
KIND_CONFIG = "CONFIG"
KIND_SHUTDOWN = "SHUTDOWN"
KIND_ERROR = "ERROR"

ALL_KINDS: tuple[str, ...] = (
    KIND_STARTUP, KIND_CONNECT, KIND_ACCOUNT_SYNC, KIND_RECONCILE, KIND_READY,
    KIND_SIGNAL, KIND_RISK_PASS, KIND_RISK_BLOCK, KIND_ORDER_SUBMIT, KIND_FILL,
    KIND_POSITION, KIND_TRADE_DONE, KIND_RECONNECT, KIND_RECOVERY, KIND_DEGRADE,
    KIND_KILL, KIND_RECOVER, KIND_CONFIG, KIND_SHUTDOWN, KIND_ERROR,
)

# 连续重复事件的合并窗口(ms)。同一 (kind, text, symbol) 在这个窗口内重复时,
# 只把上一条的计数 +1, 不新增行。
DEDUPE_WINDOW_MS = 120_000

# **只对「高频且重复本身不含信息」的事件去重。**
#
# 为什么不无差别去重: `day_summary()` 的稳定性计数是**按条数**统计的,
# 「今日发生 3 次 WS 重连」必须真的是 3 —— 合并成 1 条会让日报低报故障频次,
# 那比时间线啰嗦严重得多。所以只有下面这两类才压:
#
#   SIGNAL      策略每轮都可能重发同一信号, 用户看到的就是复读
#   RISK_BLOCK  同上, 而且是**预期行为**, 重复零信息量
#
# 其余 (重连/恢复/急停/成交/错误…) 要么本身就该计数, 要么频率天然很低, 一律不去重。
_DEDUPE_KINDS: frozenset[str] = frozenset({KIND_SIGNAL, KIND_RISK_BLOCK})

# 稳定性计数: 「今日系统复盘」要回答「WS 重连 N 次 / API 超时 N 次 / 自动恢复 N 次 /
# 人工干预 N 次」。kind -> 计数键(未列出的事件不参与稳定性统计)。
_STABILITY_KEYS: dict[str, str] = {
    KIND_RECONNECT: "ws_reconnects",
    KIND_RECOVERY: "auto_recoveries",
    KIND_DEGRADE: "degradations",
    KIND_KILL: "kills",
    KIND_ERROR: "errors",
    KIND_TRADE_DONE: "trades",
    # 当日启动次数 > 1 说明中间重启过 —— 无人值守语境下这是用户该知道的事实
    # (也可能是崩溃重启), 所以计进稳定性而不是只当作噪音。
    KIND_STARTUP: "startups",
}

# detail 里出现即掩码的键名片段(与 config_store 判据一致: 只按名字判定)
_SENSITIVE_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSPHRASE")
_MASK = "<masked>"

# 自由文本里的 `NAME_KEY=value` / `token: value`(值可带引号或无引号)形态
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:" + "|".join(_SENSITIVE_MARKERS) + r")[A-Za-z0-9_]*)"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)

# 单次 emit 最多允许 detail 多大(表列是 VARCHAR(1000), 超了截断而不是让 INSERT 失败)
_DETAIL_MAX = 1000


def _is_sensitive(key: str) -> bool:
    upper = key.upper()
    return any(m in upper for m in _SENSITIVE_MARKERS)


def scrub_detail(value: Any) -> Any:
    """递归脱敏: 敏感键 → `<masked>`; 字符串值里若内嵌 `KEY=...` 形态也一并掩码。

    **按值二次扫描**是必要的: 密钥经常不是以键的形式出现, 而是被拼进一句自由文本
    (例: 订单 `reason` 里带上完整 URL 或配置片段), 只按键名过滤会漏。
    """
    if isinstance(value, dict):
        return {
            str(k): (_MASK if _is_sensitive(str(k)) else scrub_detail(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub_detail(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def scrub_text(text: str) -> str:
    """把自由文本里形如 `SOMETHING_KEY=xxx` / `token: xxx` 的片段掩码掉。"""
    return _INLINE_SECRET_RE.sub(lambda m: f"{m.group(1)}={_MASK}", text)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _local_day_bounds(day: date_cls) -> tuple[int, int]:
    """本地日 [00:00, 次日 00:00) 的毫秒边界 —— 「今日」按操作者所在时区算, 不按 UTC。"""
    start = datetime(day.year, day.month, day.day)
    return (
        int(start.timestamp() * 1000),
        int((start + timedelta(days=1)).timestamp() * 1000),
    )


class OperatorEventLog:
    """内存环 + 落库双写的人话事件流(append-only)。"""

    def __init__(self, capacity: int = 500) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._pending: set[asyncio.Task] = set()
        self._persist_failures = 0
        self._dropped_no_loop = 0

    # ---------------------------------------------------------------- 写入

    def emit(
        self,
        kind: str,
        text: str,
        *,
        level: str = "NOTICE",
        symbol: str = "",
        ref_type: str = "",
        ref_id: str = "",
        detail: dict[str, Any] | None = None,
        ts: int | None = None,
    ) -> dict[str, Any]:
        """记一条事件(同步, **永不抛异常**)。

        先入内存环(无条件成功), 再尽力落库。落库失败只累加计数并记 warning ——
        事件流是旁挂设施, 它的故障不该让交易主链路感知。

        `_DEDUPE_KINDS` 里的高频事件在 `DEDUPE_WINDOW_MS` 内连续重复时会被**合并计数**
        而不是重复追加 —— 但稳定性计数用的那些 kind 一律不去重, 见该常量的注释。
        """
        now = ts if ts is not None else _now_ms()
        clean_text = scrub_text(str(text or ""))
        clean_kind = str(kind or "")
        clean_symbol = str(symbol or "")

        merged = self._try_merge(clean_kind, clean_text, clean_symbol, now)
        if merged is not None:
            return merged

        event = {
            "ts": now,
            "kind": clean_kind,
            "level": str(level or "NOTICE"),
            "text": clean_text,
            "symbol": clean_symbol,
            "ref_type": str(ref_type or ""),
            "ref_id": str(ref_id or ""),
            "detail": scrub_detail(detail or {}),
            "count": 1,
        }
        self._ring.append(event)
        self._schedule_persist(event)
        return event

    def _try_merge(
        self, kind: str, text: str, symbol: str, now_ms: int
    ) -> dict[str, Any] | None:
        """与环形缓冲最后一条「同 kind + 同文案 + 同 symbol 且仍在窗口内」则合并。

        只跟上一条比 —— 只压**连续**重复。中间夹了别的事件就说明情况变了, 应另起一行
        (例如「信号 → 成交 → 信号」不该被压成一条)。

        **只更新内存环, 不更新已落库的那一行** —— 合并纯属展示层的降噪, 库里的第一条
        记录仍然如实存在。代价是重启后计数归 1, 这是可接受的: 重启后「发生过这件事」
        仍然看得到, 丢的只是「重复了几次」。
        """
        if kind not in _DEDUPE_KINDS or not self._ring:
            return None
        last = self._ring[-1]
        if (last.get("kind"), last.get("text"), last.get("symbol")) != (kind, text, symbol):
            return None
        if now_ms - int(last.get("ts") or 0) > DEDUPE_WINDOW_MS:
            return None
        last["count"] = int(last.get("count") or 1) + 1
        last["ts"] = now_ms
        return last

    def _schedule_persist(self, event: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无事件循环(纯同步上下文/测试) —— 只留内存环, 不算失败。
            self._dropped_no_loop += 1
            return
        task = loop.create_task(self._persist(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _persist(self, event: dict[str, Any]) -> None:
        try:
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import OperatorEvent

            payload = json.dumps(event["detail"], ensure_ascii=False, default=str)
            async with AsyncSessionLocal() as session:
                session.add(
                    OperatorEvent(
                        ts=event["ts"],
                        kind=event["kind"],
                        level=event["level"],
                        text=event["text"],
                        symbol=event["symbol"],
                        ref_type=event["ref_type"],
                        ref_id=event["ref_id"],
                        detail=payload[:_DETAIL_MAX],
                    )
                )
                await session.commit()
        except Exception as exc:  # 旁挂设施, 绝不向上传播
            self._persist_failures += 1
            logger.warning("操作员事件落库失败(不影响交易)", kind=event.get("kind"), error=str(exc))

    async def flush(self, timeout: float = 5.0) -> None:
        """等待所有在途落库完成(测试与优雅停机用)。超时即放弃, 不阻塞停机。"""
        if not self._pending:
            return
        pending = list(self._pending)
        try:
            await asyncio.wait(pending, timeout=timeout)
        except Exception:  # pragma: no cover - 防御
            pass

    # ---------------------------------------------------------------- 读取

    def recent(self, limit: int = 50, *, kind: str = "") -> list[dict[str, Any]]:
        """读内存环(同步、无 I/O, 供 WS 秒级推送与页面首屏)。"""
        items = list(self._ring)
        if kind:
            items = [e for e in items if e["kind"] == kind]
        return items[-max(1, limit):][::-1]  # 最新在前

    async def load_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        """读库(重启后仍能看到今天早些时候发生了什么)。失败降级为内存环。"""
        try:
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import OperatorEvent
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(OperatorEvent)
                            .order_by(OperatorEvent.ts.desc())
                            .limit(max(1, min(limit, 1000)))
                        )
                    )
                    .scalars()
                    .all()
                )
            return [
                {
                    "ts": r.ts,
                    "kind": r.kind,
                    "level": r.level,
                    "text": r.text,
                    "symbol": r.symbol,
                    "ref_type": r.ref_type,
                    "ref_id": r.ref_id,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("读取操作员事件失败, 降级为内存环", error=str(exc))
            return self.recent(limit)

    async def day_summary(self, day: date_cls | None = None) -> dict[str, Any]:
        """当日汇总 —— 「今日系统复盘」的稳定性计数来源。"""
        day = day or datetime.now().date()
        start_ms, end_ms = _local_day_bounds(day)
        empty = {
            "date": day.isoformat(),
            "total": 0,
            "by_kind": {},
            "ws_reconnects": 0,
            "auto_recoveries": 0,
            "degradations": 0,
            "kills": 0,
            "errors": 0,
            "trades": 0,
            "startups": 0,
            "human_interventions": 0,
        }
        try:
            from at01_common.database import AsyncSessionLocal
            from at01_common.models import OperatorEvent
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(OperatorEvent).where(
                                OperatorEvent.ts >= start_ms, OperatorEvent.ts < end_ms
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        except Exception as exc:
            logger.warning("读取当日事件汇总失败, 返回空汇总", error=str(exc))
            return empty

        by_kind: dict[str, int] = {}
        for r in rows:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1

        out = dict(empty)
        out["total"] = len(rows)
        out["by_kind"] = by_kind
        for kind, key in _STABILITY_KEYS.items():
            out[key] = by_kind.get(kind, 0)
        # 人工干预 = 人工急停 + 人工恢复(自动的分别计入 kills / auto_recoveries 的来源侧)。
        # 这里用 detail 里的 actor 标记区分, 缺省按人工算(fail-safe: 宁可多报人工)。
        human = 0
        for r in rows:
            if r.kind not in (KIND_KILL, KIND_RECOVER):
                continue
            try:
                detail = json.loads(r.detail or "{}")
            except (TypeError, ValueError):
                detail = {}
            if str(detail.get("actor") or "human") == "human":
                human += 1
        out["human_interventions"] = human
        return out

    # ---------------------------------------------------------------- 状态

    def status(self) -> dict[str, Any]:
        """自述状态(供 /api/metrics 与 /ops 健康报告)。"""
        return {
            "buffered": len(self._ring),
            "capacity": self._ring.maxlen,
            "persist_failures": self._persist_failures,
            "dropped_no_loop": self._dropped_no_loop,
            "kinds": len(ALL_KINDS),
        }

    def clear(self) -> None:
        """清空内存环(测试用; 不动已落库的历史)。"""
        self._ring.clear()


# 全局单例: 与 `web_state.system_state` 同为进程级共享设施。
# 让任意模块都能在不持有 orchestrator 句柄的情况下记一条事件(埋点分散在 run.py/wiring/execution)。
operator_log = OperatorEventLog()
