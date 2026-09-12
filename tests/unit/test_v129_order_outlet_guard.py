"""V12.9 P0-3: 订单出口架构守卫

**背景**: `ExecutionEngine.execute()` 是全系统**唯一下单出口**, 但 `TradingGate` 闸门
判定目前写在**调用方**(`run.py::_on_signal` / `_apply_core_action`), **不在** `execute()` 内部。

这意味着它当前是「**调用方记得调闸门**」而不是「**出口本身无法绕过**」——
属**已记录的架构债**(见 `docs/audits/trading-path-audit.md` §4/§9),
按任务单「不阻塞本轮小资金验证、不做大改」保持现状。

**本测试的作用**: 把"我们审过一次"变成"**无法静默回归**" ——
一旦生产代码里出现**新的** `execute()` 调用点(即潜在的新绕过路径), 这里立刻变红,
强制后来者先解释清楚它有没有过闸门。

不测测试代码 —— 测试直接调 `execute()` 是正常的(那里没有 run.py 的闸门上下文)。
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# 生产代码(非 tests/)目录 —— 与 bootstrap 注入的包一致
PROD_DIRS = (
    "at01_common", "at10_market", "at20_analytics", "at30_strategy", "at40_portfolio",
    "at50_risk", "at60_execution", "at70_journal", "at80_backtest", "at85_optimizer",
    "at90_web",
)

# **已审查并批准**的生产调用点(文件相对路径 -> 允许的出现次数)。
# 每一条都必须过闸门; 新增任何一条都要先回答「它过闸门了吗」。
KNOWN_CALL_SITES: dict[str, int] = {
    "run.py": 2,  # 343=_on_signal(经 TradingGate) / 958=_apply_core_action(经闸门)
}


def _production_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for d in PROD_DIRS:
        for p in (ROOT / d).glob("*.py"):
            out[str(p.relative_to(ROOT)).replace("\\", "/")] = p.read_text(
                encoding="utf-8", errors="ignore"
            )
    rp = ROOT / "run.py"
    out["run.py"] = rp.read_text(encoding="utf-8", errors="ignore")
    return out


def _call_sites(src: str) -> list[str]:
    """找 `execution_engine.execute(...)` / `self.execution_engine.execute(...)`。

    刻意**不匹配** `session.execute` / `cur.execute`(那些是 SQL, 不是下单)。
    """
    return re.findall(r"(?<![\w.])(?:self\.)?execution_engine\.execute\(", src)


class TestOrderOutletUniqueness:
    def test_no_new_execute_call_sites_in_production(self):
        """生产代码里 `ExecutionEngine.execute()` 的调用点不得超出已审查清单。"""
        found: dict[str, int] = {}
        for name, src in _production_sources().items():
            n = len(_call_sites(src))
            if n:
                found[name] = n

        assert found == KNOWN_CALL_SITES, (
            "订单出口调用点发生变化。`ExecutionEngine.execute()` 是全系统唯一下单点, "
            "而 TradingGate 闸门由**调用方**负责(run.py 里先判闸门再调 execute) —— "
            "所以新增调用点等于**新增一条可能绕过闸门的路径**。\n"
            f"  已审查: {KNOWN_CALL_SITES}\n"
            f"  实际:   {found}\n"
            "请先确认新调用点是否过闸门: 过 → 更新本清单并说明; 不过 → 那是 P0 安全问题。"
        )

    def test_gate_is_checked_before_each_call_site(self):
        """两个调用点之前**必须**各有一次闸门判定 —— 这是当前实现的正确性前提。"""
        src = (ROOT / "run.py").read_text(encoding="utf-8")
        gate_calls = len(re.findall(r"can_open_position\(\)|can_reduce_position\(\)", src))
        exec_calls = len(_call_sites(src))
        assert gate_calls >= exec_calls, (
            f"闸门判定次数({gate_calls}) 少于 execute() 调用次数({exec_calls}) —— "
            "可能存在未过闸门的调用点。"
        )


class TestArchitecturalDebtIsDocumented:
    """架构债必须留在文档里 —— 否则后来者会以为闸门在出口内部。"""

    def test_audit_doc_records_the_gap(self):
        doc = ROOT / "docs" / "audits" / "trading-path-audit.md"
        assert doc.exists(), "交易链路审查报告缺失"
        text = doc.read_text(encoding="utf-8")
        assert "execute()" in text and "之外" in text, (
            "审查报告必须写明「闸门在 execute() 之外」这条结构性脆弱点"
        )

    def test_execute_docstring_points_at_the_caller_duty(self):
        """出口自己的 docstring 要提醒调用方负闸门责任。"""
        import inspect

        from at60_execution.execution_executor import ExecutionEngine

        doc = inspect.getdoc(ExecutionEngine.execute) or ""
        assert "闸门" in doc or "TradingGate" in doc, (
            "`execute()` 的 docstring 必须说明闸门由调用方负责 —— "
            "否则读代码的人会默认它是安全出口"
        )
