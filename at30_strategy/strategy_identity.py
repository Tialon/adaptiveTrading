"""
Strategy Identity(V6.0)

统一策略身份枚举 —— 禁止 buy/entry、sell/exit 双命名。

所有模块(StrategyEngine 装配表 / DecisionEngine 权重表 / 策略 name /
回测 / 落库)一律使用 StrategyType 值。
"""

from enum import Enum


class StrategyType(str, Enum):
    """策略身份(唯一命名源)"""

    ENTRY = "entry"    # 买入评分(原 BuyStrategy)
    EXIT = "exit"      # 卖出退出(原 SellStrategy)
    GRID = "grid"      # 网格
    TREND = "trend"    # 趋势
    DECISION = "decision"  # V6: 融合决策信号(strategy 字段专用)


# 策略能力声明(替代 Engine 里散落的 name/class 判断)
STRATEGY_CAPABILITIES = {
    StrategyType.ENTRY: {"can_buy": True, "can_sell": False, "bucket": "trade"},
    StrategyType.EXIT: {"can_buy": False, "can_sell": True, "bucket": "trade"},
    StrategyType.GRID: {"can_buy": True, "can_sell": True, "bucket": "trade"},
    StrategyType.TREND: {"can_buy": True, "can_sell": True, "bucket": "trade"},
    StrategyType.DECISION: {"can_buy": True, "can_sell": True, "bucket": "trade"},
}


def capability(strategy_name: str, cap: str) -> bool:
    """查询策略能力(未知策略默认无能力)"""
    try:
        st = StrategyType(strategy_name)
    except ValueError:
        # 兼容旧命名残留(容错读取, 不用于决策)
        legacy = {"buy": StrategyType.ENTRY, "sell": StrategyType.EXIT}
        st = legacy.get(strategy_name)
        if st is None:
            return False
    return STRATEGY_CAPABILITIES.get(st, {}).get(cap, False)
