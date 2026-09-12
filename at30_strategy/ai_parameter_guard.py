"""
AI 参数审批层(V8)

原则: **AI 只建议, 不交易**。参数建议经审批层校验:
- 变化幅度 ≤ 阈值 -> 自动应用(写入 RuntimeParams, 策略/定仓下一周期读取)
- 变化幅度 > 阈值 -> 不应用, 记 history(effective=False), 需人工确认

RuntimeParams 为进程内运行时参数缓存, 策略与定仓读取(带默认值兜底)。
"""

from typing import Any, Optional

from at01_common.logger import LoggerMixin


class RuntimeParams:
    """运行时参数缓存(AI 审批通过后写入, 策略/定仓读取)"""

    _data: dict[str, float] = {}

    @classmethod
    def get(cls, key: str, default: Optional[float] = None) -> Optional[float]:
        return cls._data.get(key, default)

    @classmethod
    def set(cls, key: str, value: float) -> None:
        cls._data[key] = value

    @classmethod
    def snapshot(cls) -> dict[str, float]:
        return dict(cls._data)


def parse_pct(value: Any) -> Optional[float]:
    """解析百分比: '3%' / '0.03' / 3 -> 0.03"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 100.0 if v > 1 else v
    s = str(value).strip().replace(",", ".")
    if s.endswith("%"):
        s = s[:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if v > 1 else v


class AIParameterGuard(LoggerMixin):
    """AI 参数变化审批(单次调整幅度限制)"""

    def __init__(self, max_change_pct: float = 0.10):
        self.max_change_pct = max_change_pct

    def evaluate(
        self, param_name: str, current: Optional[float], new: Optional[float]
    ) -> dict[str, Any]:
        """评估一次参数调整, 返回 {applied, value, reason}"""
        if current in (None, 0.0) or new is None or new <= 0:
            return {"applied": False, "value": current, "reason": "无基线或新值无效"}
        change = abs(new - current) / abs(current)
        if change > self.max_change_pct:
            return {
                "applied": False,
                "value": current,
                "reason": f"{param_name} 变化{change:.0%}超阈值{self.max_change_pct:.0%}, 需人工确认",
            }
        return {"applied": True, "value": new, "reason": ""}
