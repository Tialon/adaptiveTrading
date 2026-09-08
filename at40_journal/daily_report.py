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

    async def generate(
        self,
        symbol: str,
        regime: str = "",
        equity: float = 0.0,
        health: Optional[dict[str, Any]] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """生成昨日报告, 返回文件路径(失败返回 None)。

        `health` 为运行状态快照(生命周期/风险态/急停/熔断/告警 + V12 交易门/对账, 见
        _render_health); `metrics` 为 V12 §37 账户/持仓/HODL 对标指标(见 _render_account/
        _render_benchmark)。
        """
        settings = get_settings()
        since = time.time() - 86400.0
        now = datetime.now(tz=timezone.utc)
        yesterday = now - timedelta(days=1)

        trades = await self._trades_since(symbol, since)
        decisions = await self._decision_count(since)
        perf = await self._strategy_performance(symbol)
        activity = await self._order_activity(symbol, since)

        lines = self._render(
            symbol, yesterday.date(), regime, equity,
            trades, decisions, perf, health, metrics, activity,
        )

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

    async def _order_activity(self, symbol: str, since_ts: float) -> dict[str, Any]:
        """V12 §37: 当日交易活动 —— 买卖笔数(orders.side)+ 手续费(order_fills.fee_quote)。"""
        from sqlalchemy import func, select

        from at01_common.database import AsyncSessionLocal
        from at01_common.models import Order, OrderFill

        ts = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        buy = sell = 0
        fees = 0.0
        try:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(Order.side, func.count())
                        .where(Order.symbol == symbol, Order.created_at >= ts)
                        .group_by(Order.side)
                    )
                ).all()
                for side, cnt in rows:
                    if side == "BUY":
                        buy = int(cnt)
                    elif side == "SELL":
                        sell = int(cnt)
                fees = float(
                    (
                        await session.execute(
                            select(func.coalesce(func.sum(OrderFill.fee_quote), 0.0))
                            .where(OrderFill.symbol == symbol, OrderFill.created_at >= ts)
                        )
                    ).scalar_one()
                )
        except Exception:
            self.logger.exception("交易活动聚合失败")
        return {"buy_count": buy, "sell_count": sell, "fees": fees}

    # ---------- 渲染 ----------

    def _render_health(self, health: Optional[dict[str, Any]]) -> list[str]:
        """渲染运行状态(生命周期 / 风险态 / 急停 / 熔断 / 告警)—— V11.4 P1-4。

        对缺失键容错, health=None 时给出「未知」占位, 不抛异常。
        """
        h = health or {}
        lifecycle = h.get("lifecycle", "未知")
        risk_state = h.get("risk_state", "未知")
        risk_reason = h.get("risk_reason", "")
        kill_armed = bool(h.get("kill_switch_armed"))
        kill_reason = h.get("kill_switch_reason", "")
        breaker_open = bool(h.get("breaker_open"))
        breaker_reason = h.get("breaker_reason", "")
        alerts = h.get("alerts", 0)

        risk_line = f"- 风险状态: {risk_state}"
        if risk_reason:
            risk_line += f"({risk_reason})"
        kill_line = f"- 急停: {'是' if kill_armed else '否'}"
        if kill_armed and kill_reason:
            kill_line += f" — {kill_reason}"
        breaker_line = f"- 资金熔断: {'开' if breaker_open else '关'}"
        if breaker_open and breaker_reason:
            breaker_line += f" — {breaker_reason}"

        lines = [
            f"- 生命周期: {lifecycle}",
            risk_line,
            kill_line,
            breaker_line,
            f"- 活跃告警: {alerts}",
        ]

        # V12 §37: 交易门(六维) + 对账健康。缺失时优雅跳过(health 无 trading_gate 键)。
        tg = h.get("trading_gate") or {}
        if tg:

            def _allow(v) -> str:
                if v is None:
                    return "未知"
                return "放行" if v else "禁止"

            def _yn(v) -> str:
                if v is None:
                    return "未知"
                return "是" if v else "否"

            lines.append(f"- 交易门·开仓: {_allow(tg.get('can_open_position'))}")
            if tg.get("can_open_position") is False and tg.get("open_reason"):
                lines.append(f"- 开仓阻断: {tg.get('open_reason')}")
            lines.append(f"- 交易门·减仓: {_allow(tg.get('can_reduce_position'))}")
            lines.append(
                f"- 对账健康: {_yn(tg.get('reconciled'))} / "
                f"交易所: {_yn(tg.get('exchange_healthy'))} / "
                f"行情: {_yn(tg.get('market_data_healthy'))}"
            )
        return lines

    def _render_account(self, metrics: Optional[dict[str, Any]]) -> list[str]:
        """V12 §37: 账户与持仓(权益 / SOL / 现金 / 敞口 / 未实现 / 回撤)。metrics=None 容错。"""
        m = metrics or {}
        sol_qty = m.get("sol_qty", 0.0)
        usdt = m.get("usdt_cash", 0.0)
        price = m.get("price", 0.0)
        exposure = m.get("sol_exposure_pct", 0.0)
        unrealized = m.get("unrealized_pnl", 0.0)
        realized = m.get("realized_pnl", 0.0)
        drawdown = m.get("drawdown_pct", 0.0)
        return [
            "## 账户与持仓",
            "",
            f"- SOL 数量: {sol_qty:.6g}",
            f"- 现金(USDT): {usdt:,.2f}",
            f"- 最新价: {price:,.4f}",
            f"- SOL 敞口: {exposure:.2%}(上限 70%)",
            f"- 未实现盈亏: {unrealized:+,.2f}",
            f"- 已实现盈亏(累计): {realized:+,.2f}",
            f"- 回撤: {drawdown:.2%}",
        ]

    def _render_benchmark(self, metrics: Optional[dict[str, Any]]) -> list[str]:
        """V12 §24-25: HODL 对标(Adaptive / HODL / Cash 权益 + Alpha + 盈亏)。"""
        b = (metrics or {}).get("benchmark") or {}
        if not b:
            return ["## HODL 对标", "", "(未记录基线 — 主网首次接管后自动生成)"]
        return [
            "## HODL 对标",
            "",
            f"- 基线: 初始权益 {b.get('initial_equity', 0.0):,.2f} USDT / "
            f"初始 SOL {b.get('initial_sol_qty', 0.0):.6g} @ {b.get('initial_sol_price', 0.0):,.4f}",
            f"- 策略权益(Adaptive): {b.get('adaptive_equity', 0.0):,.2f}",
            f"- 持有权益(HODL): {b.get('hodl_equity', 0.0):,.2f}",
            f"- 现金权益(Cash): {b.get('cash_equity', 0.0):,.2f}",
            f"- Alpha(跑赢持有): {b.get('alpha', 0.0):+,.2f}",
            f"- 策略盈亏: {b.get('adaptive_pnl', 0.0):+,.2f} / 持有盈亏: {b.get('hodl_pnl', 0.0):+,.2f}",
        ]

    def _render_activity(
        self, activity: Optional[dict[str, Any]], trades: list[dict[str, Any]]
    ) -> list[str]:
        """V12 §37: 交易活动(买卖笔数 / 已实现盈亏 / 手续费)。"""
        a = activity or {}
        buy = a.get("buy_count", 0)
        sell = a.get("sell_count", 0)
        fees = a.get("fees", 0.0)
        total_pnl = round(sum(t["realized_pnl"] for t in trades), 2)
        return [
            "## 交易活动",
            "",
            f"- 当日买入: {buy} 笔 / 卖出: {sell} 笔",
            f"- 当日已实现盈亏: {total_pnl:+,.2f}",
            f"- 当日手续费: {fees:,.4f} USDT",
        ]

    def _render(
        self,
        symbol: str,
        date: Any,
        regime: str,
        equity: float,
        trades: list[dict[str, Any]],
        decisions: int,
        perf: list[dict[str, Any]],
        health: Optional[dict[str, Any]] = None,
        metrics: Optional[dict[str, Any]] = None,
        activity: Optional[dict[str, Any]] = None,
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
            "## 运行状态",
            "",
        ]
        lines += self._render_health(health)
        # V12 §37: 账户/持仓 + HODL 对标 + 交易活动
        lines += ["", *self._render_account(metrics), ""]
        lines += ["", *self._render_benchmark(metrics), ""]
        lines += ["", *self._render_activity(activity, trades), ""]
        lines += ["", "## 成交记录", ""]
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
