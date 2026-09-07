"""
Strategy Grouping(V9.0 M2) — 组合策略伞(薄分组层)

把 4 个底层策略(entry/grid/trend/exit)归为 3 个「组合策略」, 作为
归因 / 版本 / 优化的统一单位, 不删除/不移动底层策略类:

    Trend Swing     ← trend(趋势) + entry(评分确认)
    Mean Reversion  ← grid(区间) + entry(抄底)
    Exit Manager    ← exit(统一卖出)

entry(BuyStrategy)是共享评分器, 归入两个买入伞; 未知策略(如融合后的 "decision")
保持 "decision" 原样, 不强行归类。
"""

from typing import Optional

# 组合策略伞 -> 底层策略成员
PORTFOLIO_STRATEGIES: dict[str, dict] = {
    "trend_swing": {
        "label": "Trend Swing",
        "members": ["trend", "entry"],
    },
    "mean_reversion": {
        "label": "Mean Reversion",
        "members": ["grid", "entry"],
    },
    "exit_manager": {
        "label": "Exit Manager",
        "members": ["exit"],
    },
}

# 底层策略名 -> 伞名(entry 归入 mean_reversion 作为默认归因, 仍属两伞成员)
_MEMBER_TO_GROUP: dict[str, str] = {
    "trend": "trend_swing",
    "grid": "mean_reversion",
    "entry": "mean_reversion",
    "exit": "exit_manager",
}


def group_of(strategy_name: Optional[str]) -> str:
    """底层策略名 -> 组合策略伞名; 未知返回原值(如 "decision")"""
    if not strategy_name:
        return "decision"
    return _MEMBER_TO_GROUP.get(strategy_name, strategy_name)


def group_label(strategy_name: Optional[str]) -> str:
    """底层策略名 -> 组合策略中文/英文标签(未知返回原值)"""
    g = group_of(strategy_name)
    return PORTFOLIO_STRATEGIES.get(g, {}).get("label", g)
