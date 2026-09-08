"""V11.7 P1-4: 测试网真实执行闸门(Testnet real execution gate)

真实(非纸面)执行必须**同时**满足:
- BINANCE_TESTNET=true(绝对禁止主网)
- PAPER_TRADING=false(真实执行)
- RUN_TESTNET_TRADING=1(显式环境变量 opt-in)
- live_trading=false(live_trading_confirm != "true", 主网守卫不解除)
- 测试网 API key/secret 齐备

任一不满足 → BLOCKED(拒绝启动, 不尝试交易), 并输出明确的
`=== TESTNET PREFLIGHT ===` 报告。纸面模式无需此闸门(本就无真实下单)。

纯配置/环境判定, 可测; 不查交易所, 不硬编码 git_sha(真实 `git rev-parse HEAD`)。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_sha() -> str:
    """当前仓库 HEAD 完整 SHA(真实 `git rev-parse HEAD`, 不硬编码); git 不可用返回空串。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def testnet_preflight(
    *,
    binance_testnet: bool,
    paper_trading: bool,
    live_trading: bool,
    run_testnet_trading: str,
    credentials_present: bool,
    git_sha: str,
    symbol: str,
) -> dict:
    """测试网真实执行前置检查。返回 `{allowed, mode, blocked_reasons, report}`。

    纸面模式(paper_trading=true)视为安全, 直接 allowed(无真实下单), mode="paper";
    非纸面模式校验四项硬条件, 任一失败 → allowed=false, mode="real_testnet"。
    """
    report = {
        "symbol": symbol,
        "paper_trading": bool(paper_trading),
        "binance_testnet": bool(binance_testnet),
        "live_trading": bool(live_trading),
        "git_sha": git_sha or "",
        "credentials_present": bool(credentials_present),
    }

    if paper_trading:
        return {"allowed": True, "mode": "paper", "blocked_reasons": [], "report": report}

    reasons: list[str] = []
    if not binance_testnet:
        reasons.append("binance_testnet=false(真实执行仅允许测试网, 绝对禁止主网)")
    if live_trading:
        reasons.append("live_trading=true(主网实盘, 测试网闸门禁止)")
    if (run_testnet_trading or "").strip() != "1":
        reasons.append("RUN_TESTNET_TRADING 未显式设为 1")
    if not credentials_present:
        reasons.append("测试网 API key/secret 缺失")

    return {
        "allowed": not reasons,
        "mode": "real_testnet",
        "blocked_reasons": reasons,
        "report": report,
    }


def format_preflight_report(result: dict) -> str:
    """把 preflight 结果格式化为 `=== TESTNET PREFLIGHT ===` 报告块(纯函数)。"""
    r = result.get("report") or {}
    lines = [
        "=== TESTNET PREFLIGHT ===",
        f"symbol={r.get('symbol', '')}",
        f"paper_trading={'true' if r.get('paper_trading') else 'false'}",
        f"binance_testnet={'true' if r.get('binance_testnet') else 'false'}",
        f"live_trading={'true' if r.get('live_trading') else 'false'}",
        f"git_sha={r.get('git_sha', '')}",
        f"credentials_present={'true' if r.get('credentials_present') else 'false'}",
    ]
    if not result.get("allowed"):
        lines.append("BLOCKED: " + "; ".join(result.get("blocked_reasons") or []))
    return "\n".join(lines)
