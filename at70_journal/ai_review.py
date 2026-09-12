"""AI Review Package(V13 P0)—— 把散落的复盘数据打成**一个包**。

**这个模块解决什么**: 系统其实早就有了复盘需要的全部数据 —— `trade_records` /
`signals` / `risk_events` / `execution_events` / `strategy_performance` / HODL 基准 /
优化器 proposal。但它们**各自孤立**, 想回答「今天为什么亏了」得自己 join 五张表。
本模块按天把这些收进一个目录, 让 AI(或人)能直接读:

    review/YYYY-MM-DD/
      summary.json  trades.json  signals.json  risk.json  execution.json
      market_regime.json  performance.json  anomalies.json  strategy_version.json
      ai_review.md          ← 给人读的那一份

**要能回答的问题**(任务书原文): 今天赚了还是亏了? 为什么? 哪些信号有效? 哪些误判?
哪个市场环境表现差? 执行有没有问题? 风控有没有误杀? 策略是否过度交易? 与 HODL 比怎么样?
哪些参数值得研究?

**两条硬约束**
1. **脱敏**。包会被导出、被喂给 AI、被贴进对话 —— 密钥绝不能在里面。所有字符串都过
   `operator_events.scrub_detail`(按键名 + 按 `NAME_KEY=value` 文本形态双重过滤)。
2. **只读**。本模块**不产生任何交易动作**, 也不改任何状态; 它只读库、写 JSON/Markdown 文件。
   继续遵守红线: AI 可以分析、可以复盘、可以提建议, **不可以直接下单**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from at01_common.logger import LoggerMixin
from at01_common.operator_events import scrub_detail

# 包内文件名(与 docs/ai-review-spec.md 一致)
PACKAGE_FILES: tuple[str, ...] = (
    "summary.json",
    "trades.json",
    "signals.json",
    "risk.json",
    "execution.json",
    "market_regime.json",
    "performance.json",
    "anomalies.json",
    "strategy_version.json",
)
MARKDOWN_FILE = "ai_review.md"

# 「过度交易」的判据: 一天成交次数超过这个值就值得让 AI 看一眼
_OVERTRADING_TRADES = 20
# 单笔滑点超过这个比例算「执行质量异常」
_HIGH_SLIPPAGE = 0.003


def _day_bounds(day: date_cls) -> tuple[float, float]:
    """本地日 [00:00, 次日 00:00) 的 epoch 秒边界(与操作员事件流的「今日」口径一致)。"""
    start = datetime(day.year, day.month, day.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _in_window(ts: Any, start: float, end: float) -> bool:
    """把一个时间戳(可能是 float/int 秒, 或 datetime)判定是否落在窗口内。"""
    if ts is None:
        return False
    if isinstance(ts, datetime):
        value = ts.timestamp()
    else:
        try:
            value = float(ts)
        except (TypeError, ValueError):
            return False
        # ClosedTrade 的时间列是 **epoch 秒**; 若拿到毫秒则归一化
        if value > 1e11:
            value /= 1000.0
    return start <= value < end


@dataclass
class ReviewPeriod:
    """复盘周期(本地日)。"""

    day: date_cls
    start: float
    end: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "start_ts": self.start,
            "end_ts": self.end,
            "timezone": "local",
        }


class AIReviewBuilder(LoggerMixin):
    """按天构建 AI Review Package。"""

    def __init__(self, symbol: str = "SOLUSDT", report_root: str | Path = "review") -> None:
        self.symbol = symbol
        self.report_root = Path(report_root)

    # ------------------------------------------------------------------ 采集

    async def _load_rows(self, model: Any) -> list[Any]:
        """整表读回(这些表日增量很小; 按 created_at 过滤在 Python 侧做, 兼容多种时间列)。"""
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                return list((await session.execute(select(model))).scalars().all())
        except Exception as exc:
            self.logger.warning("复盘数据读取失败", model=getattr(model, "__name__", "?"),
                                error=str(exc))
            return []

    async def _collect_trades(self, period: ReviewPeriod) -> list[dict[str, Any]]:
        from at01_common.models import ClosedTrade

        rows = await self._load_rows(ClosedTrade)
        out: list[dict[str, Any]] = []
        for r in rows:
            if not _in_window(getattr(r, "exit_ts", None), period.start, period.end):
                continue
            entry = float(getattr(r, "entry_price", 0.0) or 0.0)
            qty = float(getattr(r, "quantity", 0.0) or 0.0)
            pnl = float(getattr(r, "realized_pnl", 0.0) or 0.0)
            cost = entry * qty
            out.append({
                "symbol": r.symbol,
                "strategy": r.strategy,
                "bucket": r.bucket,
                "entry_ts": r.entry_ts,
                "exit_ts": r.exit_ts,
                "entry_price": entry,
                "exit_price": float(getattr(r, "exit_price", 0.0) or 0.0),
                "quantity": qty,
                "realized_pnl": round(pnl, 4),
                "return_pct": round(pnl / cost * 100, 4) if cost > 0 else 0.0,
                "holding_seconds": float(getattr(r, "holding_seconds", 0.0) or 0.0),
                "max_profit": float(getattr(r, "max_profit", 0.0) or 0.0),
                "max_drawdown": float(getattr(r, "max_drawdown", 0.0) or 0.0),
                "regime": getattr(r, "regime", "") or "",
                "mistake_reason": getattr(r, "mistake_reason", "") or "",
            })
        return out

    async def _collect_signals(self, period: ReviewPeriod) -> list[dict[str, Any]]:
        from at01_common.models import Signal

        rows = await self._load_rows(Signal)
        out: list[dict[str, Any]] = []
        for r in rows:
            if not _in_window(getattr(r, "created_at", None), period.start, period.end):
                continue
            out.append({
                "id": r.id, "symbol": r.symbol, "strategy": r.strategy, "side": r.side,
                "price": r.price, "quantity": r.quantity, "score": r.score,
                "reason": r.reason or "", "status": r.status,
                "created_at": str(r.created_at),
            })
        return out

    async def _collect_risk_events(self, period: ReviewPeriod) -> list[dict[str, Any]]:
        from at01_common.models import RiskEvent

        rows = await self._load_rows(RiskEvent)
        out: list[dict[str, Any]] = []
        for r in rows:
            if not _in_window(getattr(r, "created_at", None), period.start, period.end):
                continue
            out.append({
                "event_type": r.event_type, "symbol": r.symbol,
                "detail": r.detail or "", "equity": r.equity,
                "created_at": str(r.created_at),
            })
        return out

    async def _collect_execution(self, period: ReviewPeriod) -> list[dict[str, Any]]:
        from at01_common.models import ExecutionEvent

        rows = await self._load_rows(ExecutionEvent)
        out: list[dict[str, Any]] = []
        for r in rows:
            if not _in_window(getattr(r, "event_time", None), period.start, period.end):
                continue
            try:
                payload = json.loads(getattr(r, "payload", "{}") or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append({
                "event_type": r.event_type, "client_order_id": r.client_order_id,
                "exchange_order_id": r.exchange_order_id, "source": r.source,
                "sequence": r.sequence, "event_time": r.event_time, "payload": payload,
            })
        return out

    async def _collect_operator_events(self, period: ReviewPeriod) -> dict[str, Any]:
        """操作员事件流的当日汇总(稳定性计数) —— 与首页「今日系统复盘」同源。"""
        from at01_common.operator_events import operator_log

        try:
            return await operator_log.day_summary(period.day)
        except Exception:
            return {}

    async def _collect_strategy_version(self) -> dict[str, Any]:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyVersion

        try:
            async with AsyncSessionLocal() as session:
                rows = list((await session.execute(select(StrategyVersion))).scalars().all())
        except Exception:
            return {}
        active = next((r for r in rows if r.active), None)
        latest = max(rows, key=lambda r: r.id) if rows else None
        chosen = active or latest
        if chosen is None:
            return {}
        try:
            params = json.loads(chosen.params or "{}")
        except (TypeError, ValueError):
            params = {}
        return {
            "version": chosen.version,
            "activated": bool(chosen.active),
            "note": chosen.note or "",
            "params": params,
            "created_at": str(chosen.created_at),
        }

    async def _collect_performance(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        """当日绩效。与 HODL 的对比复用既有 `HodlBenchmark`, 不另算一套。"""
        wins = [t for t in trades if t["realized_pnl"] > 0]
        losses = [t for t in trades if t["realized_pnl"] < 0]
        gross_win = sum(t["realized_pnl"] for t in wins)
        gross_loss = abs(sum(t["realized_pnl"] for t in losses))
        out: dict[str, Any] = {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
            "total_pnl": round(sum(t["realized_pnl"] for t in trades), 4),
            "gross_profit": round(gross_win, 4),
            "gross_loss": round(gross_loss, 4),
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
            "avg_holding_seconds": (
                round(sum(t["holding_seconds"] for t in trades) / len(trades), 1)
                if trades else 0.0
            ),
            "best_trade": max((t["realized_pnl"] for t in trades), default=0.0),
            "worst_trade": min((t["realized_pnl"] for t in trades), default=0.0),
        }
        try:
            from at70_journal.hodl_benchmark import HodlBenchmark

            bench = HodlBenchmark(self.symbol)
            baseline = await bench.load_baseline()
            out["hodl_baseline_available"] = baseline is not None
        except Exception:
            out["hodl_baseline_available"] = False
        out["benchmark"] = await self._benchmark_snapshot()
        return out

    async def _benchmark_snapshot(self) -> dict[str, Any] | None:
        """HODL 对标快照。拿不到就返回 None —— 不能编一个「跑赢 HODL」出来。"""
        try:
            from at01_common.settings import get_settings

            settings = get_settings()
            if not hasattr(settings, "symbol_list"):
                return None
            from at70_journal.hodl_benchmark import HodlBenchmark

            bench = HodlBenchmark(self.symbol)
            if not await bench.has_baseline():
                return None
            baseline = await bench.load_baseline()
            return {"baseline": baseline}
        except Exception:
            return None

    # ------------------------------------------------------------------ 派生

    @staticmethod
    def _market_regimes(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按市场环境聚合当日表现 —— 回答「哪个市场环境表现差」。"""
        buckets: dict[str, dict[str, Any]] = {}
        for t in trades:
            regime = t["regime"] or "UNKNOWN"
            b = buckets.setdefault(regime, {"regime": regime, "trades": 0, "pnl": 0.0, "wins": 0})
            b["trades"] += 1
            b["pnl"] = round(b["pnl"] + t["realized_pnl"], 4)
            if t["realized_pnl"] > 0:
                b["wins"] += 1
        out = list(buckets.values())
        for b in out:
            b["win_rate"] = round(b["wins"] / b["trades"], 4) if b["trades"] else 0.0
        return sorted(out, key=lambda b: b["pnl"])

    @staticmethod
    def _by_strategy(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按策略聚合 —— 回答「哪个策略在赚、哪个在亏」。"""
        buckets: dict[str, dict[str, Any]] = {}
        for t in trades:
            key = t["strategy"] or "unknown"
            b = buckets.setdefault(key, {"strategy": key, "trades": 0, "pnl": 0.0, "wins": 0})
            b["trades"] += 1
            b["pnl"] = round(b["pnl"] + t["realized_pnl"], 4)
            if t["realized_pnl"] > 0:
                b["wins"] += 1
        out = list(buckets.values())
        for b in out:
            b["win_rate"] = round(b["wins"] / b["trades"], 4) if b["trades"] else 0.0
        return sorted(out, key=lambda b: -b["pnl"])

    @staticmethod
    def _anomalies(
        trades: list[dict[str, Any]], signals: list[dict[str, Any]],
        risk_events: list[dict[str, Any]], execution: list[dict[str, Any]],
        stability: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """当日值得 AI 看的问题清单。**只描述现象, 不下结论** —— 结论是 AI 的活。"""
        out: list[dict[str, Any]] = []

        skipped = [s for s in signals if s["status"] == "rejected"]
        if skipped:
            out.append({
                "kind": "signals_rejected",
                "text": f"{len(skipped)} 个信号未被采纳",
                "count": len(skipped),
                "hint": "看风控事件判断是误杀还是正确拦截。",
            })

        unknown = [e for e in execution if e["event_type"] == "UNKNOWN"]
        if unknown:
            out.append({
                "kind": "order_unknown",
                "text": f"{len(unknown)} 次订单状态未知(超时/未确认)",
                "count": len(unknown),
                "hint": "核对交易所是否实际成交; 这类事件直接关系账本准确性。",
            })

        recovered = [e for e in execution if e["event_type"] in ("RECOVERY", "RECOVERED")]
        if recovered:
            out.append({
                "kind": "order_recovery",
                "text": f"{len(recovered)} 次订单恢复动作",
                "count": len(recovered),
                "hint": "正常收敛可忽略; 频次升高说明交易所交互不稳定。",
            })

        if len(trades) > _OVERTRADING_TRADES:
            out.append({
                "kind": "possible_overtrading",
                "text": f"当日成交 {len(trades)} 次, 高于参考阈值 {_OVERTRADING_TRADES}",
                "count": len(trades),
                "hint": "对比手续费与净收益, 判断是否为无效高频。",
            })

        heavy = [t for t in trades if t["max_drawdown"] and t["max_profit"]
                 and t["realized_pnl"] < 0 and t["max_profit"] > abs(t["max_drawdown"])]
        if heavy:
            out.append({
                "kind": "gave_back_profit",
                "text": f"{len(heavy)} 笔交易曾浮盈可观但最终亏损",
                "count": len(heavy),
                "hint": "看退出逻辑(移动止盈阈值)是否偏松。",
            })

        for kind, key in (("kill_switches", "kills"), ("degradations", "degradations"),
                          ("errors", "errors"), ("ws_reconnects", "ws_reconnects")):
            n = int(stability.get(key) or 0)
            if n:
                out.append({"kind": kind, "text": f"当日 {kind} 计 {n} 次", "count": n,
                            "hint": "看是否需要调整重试/退避或排查外部依赖。"})

        if risk_events:
            out.append({
                "kind": "risk_events",
                "text": f"当日风控事件 {len(risk_events)} 条",
                "count": len(risk_events),
                "hint": "区分「正确拦截」与「误杀」—— 后者是策略参数的线索。",
            })
        return out

    @staticmethod
    def _recommendation_candidates(anomalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """**候选**研究课题(不是结论) —— 任务书: AI 可提建议, 但需人工确认后才成为新版本。"""
        return [
            {"from": a["kind"], "topic": a["hint"], "count": a["count"]}
            for a in anomalies
        ]

    # ------------------------------------------------------------------ 组装

    async def build(self, day: date_cls | None = None) -> dict[str, Any]:
        """构建当日复盘包(纯读 + 纯计算, 不写任何文件)。"""
        day = day or datetime.now().date()
        start, end = _day_bounds(day)
        period = ReviewPeriod(day=day, start=start, end=end)

        trades = await self._collect_trades(period)
        signals = await self._collect_signals(period)
        risk_events = await self._collect_risk_events(period)
        execution = await self._collect_execution(period)
        stability = await self._collect_operator_events(period)
        strategy_version = await self._collect_strategy_version()
        performance = await self._collect_performance(trades)
        regimes = self._market_regimes(trades)
        anomalies = self._anomalies(trades, signals, risk_events, execution, stability)

        package: dict[str, Any] = {
            "period": period.to_dict(),
            "system_status": {
                "symbol": self.symbol,
                "stability": stability,
                "generated_at": time.time(),
            },
            "performance": performance,
            "performance_by_strategy": self._by_strategy(trades),
            "trades": trades,
            "signals": signals,
            "risk_events": risk_events,
            "execution_events": execution,
            "market_regimes": regimes,
            "anomalies": anomalies,
            "strategy_version": strategy_version,
            "recommendation_candidates": self._recommendation_candidates(anomalies),
        }
        # 脱敏是硬约束: 包会被导出、被喂给 AI、被贴进对话 —— 再过一遍才放心。
        return scrub_detail(package)

    # ------------------------------------------------------------------ 输出

    def split(self, package: dict[str, Any]) -> dict[str, Any]:
        """按任务书指定的文件集切分(每个文件一个主题, 便于 AI 按需读取)。"""
        perf = dict(package.get("performance") or {})
        perf["by_strategy"] = package.get("performance_by_strategy") or []
        return {
            "summary.json": {
                "period": package.get("period"),
                "system_status": package.get("system_status"),
                "performance": perf,
                "strategy_version": package.get("strategy_version"),
                "anomaly_count": len(package.get("anomalies") or []),
            },
            "trades.json": {"trades": package.get("trades") or []},
            "signals.json": {"signals": package.get("signals") or []},
            "risk.json": {"risk_events": package.get("risk_events") or []},
            "execution.json": {"execution_events": package.get("execution_events") or []},
            "market_regime.json": {"market_regimes": package.get("market_regimes") or []},
            "performance.json": perf,
            "anomalies.json": {
                "anomalies": package.get("anomalies") or [],
                "recommendation_candidates": package.get("recommendation_candidates") or [],
            },
            "strategy_version.json": package.get("strategy_version") or {},
        }

    async def write_package(
        self, day: date_cls | None = None, root: str | Path | None = None
    ) -> dict[str, Any]:
        """把复盘包写到 `review/YYYY-MM-DD/`。返回 `{dir, files, package}`。"""
        day = day or datetime.now().date()
        package = await self.build(day)
        base = Path(root) if root is not None else self.report_root
        target = base / day.isoformat()
        target.mkdir(parents=True, exist_ok=True)

        files: list[str] = []
        for name, payload in self.split(package).items():
            (target / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            files.append(name)
        (target / MARKDOWN_FILE).write_text(self.render_markdown(package), encoding="utf-8")
        files.append(MARKDOWN_FILE)

        self.logger.info("AI 复盘包已生成", day=day.isoformat(), files=len(files),
                         anomalies=len(package.get("anomalies") or []))
        return {"dir": str(target), "files": files, "package": package}

    def latest_dir(self, root: str | Path | None = None) -> Path | None:
        """最近一份复盘包目录(按目录名即日期的字典序取最大)。"""
        base = Path(root) if root is not None else self.report_root
        if not base.is_dir():
            return None
        candidates = sorted(
            (p for p in base.iterdir() if p.is_dir() and _looks_like_date(p.name)),
            key=lambda p: p.name,
        )
        return candidates[-1] if candidates else None

    # ------------------------------------------------------------------ 人读版

    def render_markdown(self, package: dict[str, Any]) -> str:
        """`ai_review.md` —— 给人看的那一份, 结构对齐任务书的「今日系统复盘」。"""
        period = package.get("period") or {}
        perf = package.get("performance") or {}
        strat = package.get("performance_by_strategy") or []
        regimes = package.get("market_regimes") or []
        anomalies = package.get("anomalies") or []
        stability = ((package.get("system_status") or {}).get("stability")) or {}
        version = package.get("strategy_version") or {}

        lines: list[str] = []
        lines.append(f"# 系统复盘 {period.get('date', '')}")
        lines.append("")
        lines.append("> 本文由 `at70_journal/ai_review.py` 自动生成。")
        lines.append("> **AI 只做分析与建议, 不直接下单** —— 任何参数变更都需人工确认后成为新版本。")
        lines.append("")

        lines.append("## 结论")
        lines.append("")
        trades = int(perf.get("trades") or 0)
        pnl = float(perf.get("total_pnl") or 0.0)
        if trades == 0:
            lines.append("今天没有成交。")
        else:
            verdict = "赚了" if pnl > 0 else ("亏了" if pnl < 0 else "持平")
            lines.append(
                f"今天共 {trades} 笔交易, 净盈亏 **{pnl:+.2f}**, 整体{verdict}。"
            )
            lines.append("")
            lines.append(f"- 胜率: {float(perf.get('win_rate') or 0) * 100:.1f}% "
                         f"({perf.get('wins', 0)} 胜 / {perf.get('losses', 0)} 负)")
            pf = perf.get("profit_factor")
            lines.append(f"- 盈亏比: {pf if pf is not None else '无亏损, 不适用'}")
            lines.append(f"- 单笔最佳 {float(perf.get('best_trade') or 0):+.2f} / "
                         f"最差 {float(perf.get('worst_trade') or 0):+.2f}")
            lines.append(f"- 平均持仓: {float(perf.get('avg_holding_seconds') or 0) / 60:.1f} 分钟")
        lines.append("")

        lines.append("## 策略表现")
        lines.append("")
        if strat:
            lines.append("| 策略 | 次数 | 胜率 | 盈亏 |")
            lines.append("|------|-----:|-----:|-----:|")
            for s in strat:
                lines.append(f"| {s['strategy']} | {s['trades']} | "
                             f"{float(s['win_rate']) * 100:.0f}% | {float(s['pnl']):+.2f} |")
        else:
            lines.append("(今日无成交)")
        lines.append("")

        lines.append("## 市场环境")
        lines.append("")
        if regimes:
            lines.append("| 环境 | 次数 | 胜率 | 盈亏 |")
            lines.append("|------|-----:|-----:|-----:|")
            for r in regimes:
                lines.append(f"| {r['regime']} | {r['trades']} | "
                             f"{float(r['win_rate']) * 100:.0f}% | {float(r['pnl']):+.2f} |")
            lines.append("")
            lines.append(f"(按盈亏升序 —— 表首即当日表现最差的环境: **{regimes[0]['regime']}**)")
        else:
            lines.append("(今日无成交)")
        lines.append("")

        lines.append("## 系统稳定性")
        lines.append("")
        lines.append(f"- 行情重连: {stability.get('ws_reconnects', 0)} 次")
        lines.append(f"- 自动恢复: {stability.get('auto_recoveries', 0)} 次")
        lines.append(f"- 降级: {stability.get('degradations', 0)} 次")
        lines.append(f"- 急停: {stability.get('kills', 0)} 次")
        lines.append(f"- 人工干预: {stability.get('human_interventions', 0)} 次")
        lines.append(f"- 错误: {stability.get('errors', 0)} 次")
        lines.append("")

        lines.append("## 问题")
        lines.append("")
        if anomalies:
            for i, a in enumerate(anomalies, 1):
                lines.append(f"{i}. {a['text']}")
        else:
            lines.append("未发现值得注意的问题。")
        lines.append("")

        lines.append("## 建议 AI 进一步研究")
        lines.append("")
        cands = package.get("recommendation_candidates") or []
        if cands:
            for c in cands:
                lines.append(f"- {c['topic']}")
        else:
            lines.append("- (今日无特别线索)")
        lines.append("")

        lines.append("## 策略版本")
        lines.append("")
        if version:
            from at30_strategy.strategy_version import is_auto_note

            kind = "已激活版本" if version.get("activated") else "启动基线(未人工激活)"
            lines.append(f"- 版本: `{version.get('version')}` ({kind})")
            # 机器标识(英文 note)对读者没有信息量, 只展示人工写下的备注
            note = version.get("note") or ""
            if note and not is_auto_note(note):
                lines.append(f"- 备注: {note}")
        else:
            lines.append("- (无版本快照)")
        lines.append("")

        lines.append("## 与 HODL 对比")
        lines.append("")
        bench = perf.get("benchmark")
        if bench and bench.get("baseline"):
            lines.append(f"- HODL 基线: {bench['baseline']}")
        else:
            lines.append("- **尚未建立 HODL 基准**(接管时刻未记录), 因此今日无法给出 Alpha。")
            lines.append("  —— 如实标注, 不用其它数字代替。")
        lines.append("")
        return "\n".join(lines)


def _looks_like_date(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
        return True
    except ValueError:
        return False
