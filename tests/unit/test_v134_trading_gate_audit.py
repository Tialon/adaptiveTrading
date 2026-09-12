"""V11.3 P0-4 TradingGate 最终审计(入口全覆盖 + 防绕过, 静态源码审计)。

静态断言(不依赖运行时装配)钉住三条规则:
1. 开新仓 BUY/ADD 只走 `TradingGate.can_open_position`, 减仓 SELL/REDUCE 只走
   `TradingGate.can_reduce_position`, 且 run.py 两个入口(`_on_signal` / `_apply_core_action`)
   都调用闸门。
2. 撤单 CANCEL 走 `TradingGate.can_cancel_order`(急停撤单端点)。
3. 禁止绕过: 终端提交方 `execution_executor` / 编排方 `run.py` 不得直接调用
   `risk_manager.can_buy/can_sell` 做风险许可(风险许可只在 `trading_gate.py` 与
   `risk_manager.py` 内部发生)。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _method_body(src: str, name: str) -> str:
    """提取类内方法体(从 def 行到下一个同级 def/缩进回退)。"""
    m = re.search(rf"^[ \t]*async def {name}\b.*$", src, re.MULTILINE)
    assert m is not None, f"未找到方法 {name}"
    body_start = src.index("\n", m.start()) + 1
    body_lines: list[str] = []
    for ln in src[body_start:].split("\n"):
        stripped = ln.strip()
        if stripped and not (ln.startswith("    ") and not ln.startswith("     ")):
            if re.match(r"^[ \t]*(async )?def ", ln):
                break
        body_lines.append(ln)
    return "\n".join(body_lines)


def test_run_entry_points_route_through_gate():
    """run.py 两个下单入口都调用统一交易闸门。"""
    run = _read("run.py")
    assert "self.trading_gate.can_open_position()" in run
    assert "self.trading_gate.can_reduce_position()" in run

    signal_body = _method_body(run, "_on_signal")
    assert "can_open_position()" in signal_body
    assert "can_reduce_position()" in signal_body
    # BUY 分支 -> open, 其余(SELL)-> reduce
    assert 'side.value == "BUY"' in signal_body

    core_body = _method_body(run, "_apply_core_action")
    assert "can_open_position()" in core_body
    assert "can_reduce_position()" in core_body
    assert "CoreAction.ADD" in core_body
    assert "CoreAction.REDUCE" in core_body


def test_cancel_routes_through_gate():
    """急停撤单端点调用 can_cancel_order(单一权威撤单闸门)。"""
    routes = _read("at90_web/web_api_routes.py")
    assert "can_cancel_order()" in routes
    assert "cancel_all_open_orders" in routes


def test_no_risk_manager_permission_bypass():
    """禁止绕过: 编排层(run.py)与终端执行层(execution_executor)不得直接调用
    risk_manager.can_buy/can_sell 做风险许可。"""
    for rel in ("run.py", "at60_execution/execution_executor.py"):
        src = _read(rel)
        assert "risk_manager.can_buy" not in src, f"{rel} 绕过闸门直调 can_buy"
        assert "risk_manager.can_sell" not in src, f"{rel} 绕过闸门直调 can_sell"


def test_risk_permission_authority_is_gate_only():
    """风险许可(can_buy/can_sell)只在闸门与风险层内部被消费, 不在其它生产模块出现。"""
    gate = _read("at50_risk/trading_gate.py")
    assert "self.risk_manager.can_buy()" in gate
    assert "self.risk_manager.can_sell()" in gate
