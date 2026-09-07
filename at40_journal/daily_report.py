"""
Daily Report(V9.0) — 每日自动复盘

聚合 decision_log / trade_records / strategy_performance, 生成 reports/YYYY-MM-DD.md:
昨日行情与系统状态、成交记录、盈利/失败交易、策略绩效、下一步。

无人值守的核心价值: 每天醒来有一份可读的"昨天系统做了什么、赚亏在哪"。
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from at01_common.logger import LoggerMixin
from at01_common.settings import get_settings


class DailyReport(LoggerMixin):
    """每日复盘报告生成器"""

    async def generate(self, symbol: str, regime: str = "", equity: float = 0.0) -> Optional[str]:
        """生成昨日报告, 返回文件路径(失败返回 None)"""
        settings = get_settings()
        since = time.time() - 86400.0
        now = datetime.now(tz=timezone.utc)
        yesterday = now - timedelta(days=1)

        trades = await self._trades_since(symbol, since)
        decisions = await self._decision_count(since)
        perf = await self._strategy_performance(symbol)

        lines = self._render(symbol, yesterday.date(), regime, equity, trades, decisions, perf)

        try:
            out_dir = Path(settings.daily_report_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{yesterday.date().isoformat()}.md"
            path.write_text("\n".join(lines), encoding="utf-8")
            self.logger.info("每日复盘已生成", path=str(path))
            return str(path)
        except Exception:
            self.logger.exception("每日复盘生成失败")
            return None

    # ---------- 数据聚合 ----------

    async def _trades_since(self, symbol: str, since_ts: float) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import ClosedTrade

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    (
                        await session.execute(
                            select(ClosedTrade)
                            .where(
                                ClosedTrade.symbol == symbol,
                                ClosedTrade.exit_ts >= since_ts,
                            )
                            .order_by(ClosedTrade.id.desc())
                        )
                    )
                    .scalars()
                    .all()
                )
                return [
                    {
                        "strategy": r.strategy, "bucket": r.bucket,
                        "entry_price": r.entry_price, "exit_price": r.exit_price,
                        "quantity": r.quantity, "realized_pnl": r.realized_pnl,
                        "holding_hours": round(r.holding_seconds / 3600.0, 1),
                        "max_profit": r.max_profit, "max_drawdown": r.max_drawdown,
                    }
                    for r in rows
                ]
        except Exception:
            return []

    async def _decision_count(self, since_ts: float) -> int:
        from sqlalchemy import func, select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import DecisionLog

        try:
            async with AsyncSessionLocal() as session:
                ts = datetime.fromtimestamp(since_ts, tz=timezone.utc)
                return int(
                    (
                        await session.execute(
                            select(func.count()).select_from(DecisionLog)
                            .where(DecisionLog.created_at >= ts)
                        )
                    ).scalar_one()
                )
        except Exception:
            return 0

    async def _strategy_performance(self, symbol: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import StrategyPerformance

        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(StrategyPerformance).where(StrategyPerformance.symbol == symbol)
                    )
                ).scalars().all()
                return [
                    {
                        "strategy": r.strategy, "trades": r.trade_count,
                        "win_rate": round(r.win_rate, 3), "profit": round(r.profit, 2),
                    }
                    for r in rows
                ]
        except Exception:
            return []

    # ---------- 渲染 ----------

    def _render(
        self,
        symbol: str,
        date: Any,
        regime: str,
        equity: float,
        trades: list[dict[str, Any]],
        decisions: int,
        perf: list[dict[str, Any]],
    ) -> list[str]:
        total_pnl = round(sum(t["realized_pnl"] for t in trades), 2)
        wins = [t for t in trades if t["realized_pnl"] > 0]
        losses = [t for t in trades if t["realized_pnl"] <= 0]

        lines = [
            f"# 每日复盘 {date}",
            "",
            f"- 标的: {symbol}",
            f"- 市场环境: {regime or '未知'}",
            f"- 权益: {equity:,.2f} USDT",
            f"- 当日决策次数: {decisions}",
            f"- 当日成交: {len(trades)} 笔(盈利 {len(wins)} / 亏损 {len(losses)}), 已实现盈亏 {total_pnl:+,.2f}",
            "",
            "## 成交记录",
            "",
        ]
        if not trades:
            lines.append("(当日无成交)")
        else:
            lines.append("| 策略 | 仓 | 买入价 | 卖出价 | 数量 | 盈亏 | 持仓(h) | 最大浮盈 | 最大回撤 |")
            lines.append("|------|----|--------|--------|------|------|---------|----------|----------|")
            for t in trades:
                lines.append(
                    f"| {t['strategy']} | {t['bucket']} | {t['entry_price']:.4g} "
                    f"| {t['exit_price']:.4g} | {t['quantity']:.4g} "
                    f"| {t['realized_pnl']:+,.2f} | {t['holding_hours']} "
                    f"| {t['max_profit']:.2f} | {t['max_drawdown']:.2f} |"
                )

        lines += ["", "## 策略绩效", ""]
        if not perf:
            lines.append("(暂无绩效数据)")
        else:
            lines.append("| 策略 | 笔数 | 胜率 | 累计盈亏 |")
            lines.append("|------|------|------|----------|")
            for p in perf:
                lines.append(f"| {p['strategy']} | {p['trades']} | {p['win_rate']:.0%} | {p['profit']:+,.2f} |")

        lines += ["", "## 下一步", "", "- (待 AI 优化器接入后自动生成)"]
        return lines
