"""V11.8 §21: 主网就绪自检(Mainnet Readiness Check)

主网(`BINANCE_TESTNET=false`)启动前强制自检, 任一不满足 → BLOCKED(拒绝启动),
绝不「带着未完成项静默进入主网」。与 `testnet_gate`(测试网真实执行闸门)互补:
- 测试网真实执行 → `testnet_gate`(V11.7 P1-4);
- 主网真实执行 → 本模块(默认禁主网, 见 `settings.mainnet_blocked_reason()`)。

检查项(可本地判定的确定性项, 纯函数可测):
- 主网而非测试网(base_url 为 api.binance.com 且不含 testnet);
- 真实交易(paper=false);
- 显式确认(live_trading_confirm=true);
- API key 权限已人工确认(spot-only、关提现/资金转移)—— Binance 无法 API 自证权限,
  故为操作者核对后的显式开关 `MAINNET_API_SCOPE_CONFIRMED`, 默认 false → BLOCKED;
- 冻结单币 SOLUSDT(spot);
- 配置审计通过(validate() 无问题);
- kill switch 未冻结;
- git_sha 非空(证据可追溯)。

不做真实交易所网络调用(连通性/权限属实际 go/no-go 复审, 见 docs/mainnet-readiness.md)。
"""

from __future__ import annotations

from at01_common.settings import SUPPORTED_SYMBOLS


def mainnet_readiness_check(
    *,
    binance_testnet: bool,
    paper_trading: bool,
    live_trading_confirm: str,
    api_scope_confirmed: bool,
    symbol: str,
    config_problems: list[str],
    kill_switch_armed: bool,
    git_sha: str,
    base_url: str,
) -> dict:
    """主网就绪自检。返回 `{allowed, blocked_reasons, report}`。

    所有检查均为本地确定性判定(不查交易所), 供启动前 fail-fast 与测试覆盖。
    """
    report = {
        "symbol": symbol,
        "binance_testnet": bool(binance_testnet),
        "paper_trading": bool(paper_trading),
        "live_trading_confirm": bool(
            live_trading_confirm.strip().lower() == "true"
        ),
        "api_scope_confirmed": bool(api_scope_confirmed),
        "config_ok": not config_problems,
        "kill_switch_armed": bool(kill_switch_armed),
        "git_sha": git_sha or "",
        "base_url": base_url,
    }

    reasons: list[str] = []

    # 1. 必须是主网(本自检只针对主网; 测试网走 testnet_gate)
    if binance_testnet:
        reasons.append("binance_testnet=true(主网自检仅在 BINANCE_TESTNET=false 时进行)")

    # 2. 真实交易(非纸面)
    if paper_trading:
        reasons.append("paper_trading=true(主网自检要求真实交易 PAPER_TRADING=false)")

    # 3. 显式确认主网
    if live_trading_confirm.strip().lower() != "true":
        reasons.append("LIVE_TRADING_CONFIRM 未显式设为 true(默认禁主网)")

    # 4. API key 权限人工确认(spot-only / 关提现 / 关资金转移)
    if not api_scope_confirmed:
        reasons.append(
            "MAINNET_API_SCOPE_CONFIRM 未设 true(主网 API key 权限未人工确认: 仅 Spot、"
            "关提现/资金转移)"
        )

    # 5. 冻结单币 SOLUSDT(spot)
    sym = (symbol or "").strip().upper()
    if sym not in SUPPORTED_SYMBOLS:
        reasons.append(
            f"标的 {sym!r} 不在冻结单币清单 {SUPPORTED_SYMBOLS}(仅 spot SOLUSDT)"
        )

    # 6. 配置审计通过
    if config_problems:
        reasons.append("配置审计未通过: " + "; ".join(config_problems[:5]))

    # 7. kill switch 未冻结
    if kill_switch_armed:
        reasons.append("kill switch 处于冻结状态(需人工 recover 后重新启动)")

    # 8. git_sha 可追溯
    if not git_sha:
        reasons.append("git_sha 为空(证据无法追溯代码版本)")

    # 9. 主网端点(非测试网)
    if "api.binance.com" not in (base_url or "") or "testnet" in (base_url or ""):
        reasons.append(f"主网端点 base_url 非法: {base_url!r}(需 api.binance.com)")

    return {"allowed": not reasons, "blocked_reasons": reasons, "report": report}


def format_readiness_report(result: dict) -> str:
    """把自检结果格式化为 `=== MAINNET READINESS ===` 报告块(纯函数)。"""
    r = result.get("report") or {}
    lines = [
        "=== MAINNET READINESS ===",
        f"symbol={r.get('symbol', '')}",
        f"binance_testnet={'true' if r.get('binance_testnet') else 'false'}",
        f"paper_trading={'true' if r.get('paper_trading') else 'false'}",
        f"live_trading_confirm={'true' if r.get('live_trading_confirm') else 'false'}",
        f"api_scope_confirmed={'true' if r.get('api_scope_confirmed') else 'false'}",
        f"config_ok={'true' if r.get('config_ok') else 'false'}",
        f"kill_switch_armed={'true' if r.get('kill_switch_armed') else 'false'}",
        f"git_sha={r.get('git_sha', '')}",
        f"base_url={r.get('base_url', '')}",
    ]
    if not result.get("allowed"):
        lines.append("BLOCKED: " + "; ".join(result.get("blocked_reasons") or []))
    return "\n".join(lines)
