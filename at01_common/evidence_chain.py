"""V11.7 P1-3: Testnet execution evidence chain(证据链)

把一次 Testnet 运行的所有证据串成一条可审计链路:

    run_id → order → order_fill → position → position_lot → sell_allocation
           → exchange_truth → reconciliation → soak_result

纯逻辑(可测), 不新增数据库表; 输入复用已有表(Order/OrderFill/Position/
PositionLot/SellAllocation)与 P0-4 evidence 文件(metadata.json/summary.json)。
目标不是增加功能, 而是让一次运行能完整回答:

    何时启动 / 哪个 commit / 什么模式 / 下了什么单 / 成交多少 / 手续费多少 /
    持仓多少 / 交易所余额 / 本地余额 / 对账是否 PASS / 最终 soak PASS/FAIL。

- `build_evidence_chain`: 组装 + 汇总 answers(纯函数);
- `chain_consistency_issues`: 校验链路自洽(成交覆盖/方向、lot 反推、卖出分配、
  净成交=持仓、对账、soak 验收), 返回问题列表(空 = 自洽);
- `load_run_evidence`: 从 P0-4 run_dir 读 metadata/summary 提取 run 级事实(纯文件读)。
"""

from __future__ import annotations

import json
from pathlib import Path

# 成交/持仓数量比对容差(浮点)
_TOL = 1e-6

_VALID_SOAK_VERDICTS = {"PASS", "FAIL", "BLOCKED", "NOT_EXECUTED"}


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def build_evidence_chain(
    *,
    run_id: str,
    git_sha: str,
    start_time: str,
    symbol: str,
    paper_trading: bool,
    binance_testnet: bool,
    orders: list[dict],
    fills: list[dict],
    position: dict,
    lots: list[dict],
    sell_allocations: list[dict],
    exchange_truth: dict | None = None,
    reconciliation: dict | None = None,
    soak_result: dict | None = None,
) -> dict:
    """把各环节证据组装为单一证据链 dict(纯函数), 并汇总可直接回答审计问题的 answers。"""
    orders = list(orders or [])
    fills = list(fills or [])
    lots = list(lots or [])
    sell_allocations = list(sell_allocations or [])
    position = dict(position or {})
    exchange_truth = dict(exchange_truth or {})
    reconciliation = dict(reconciliation or {})
    soak_result = dict(soak_result or {})

    total_filled = round(sum(float(o.get("filled_quantity") or 0.0) for o in orders), 8)
    total_fee_quote = round(sum(float(f.get("fee_quote") or 0.0) for f in fills), 8)
    total_commission = round(sum(float(f.get("commission") or 0.0) for f in fills), 8)

    return {
        "run_id": run_id,
        "git_sha": git_sha,
        "start_time": start_time,
        "symbol": symbol,
        "mode": {
            "paper_trading": bool(paper_trading),
            "binance_testnet": bool(binance_testnet),
        },
        "orders": orders,
        "fills": fills,
        "position": position,
        "lots": lots,
        "sell_allocations": sell_allocations,
        "exchange_truth": exchange_truth,
        "reconciliation": reconciliation,
        "soak_result": soak_result,
        "answers": {
            "order_count": len(orders),
            "fill_count": len(fills),
            "filled_quantity": total_filled,
            "total_fee_quote": total_fee_quote,
            "total_commission": total_commission,
            "position_quantity": float(position.get("quantity") or 0.0),
            "reconciliation_pass": bool(
                reconciliation.get("reconciled") or reconciliation.get("pass")
            ),
            "soak_verdict": soak_result.get("result") or "NOT_EXECUTED",
        },
    }


# ---------------------------------------------------------------------------
# 一致性校验
# ---------------------------------------------------------------------------


def chain_consistency_issues(chain: dict, tolerance: float = _TOL) -> list[str]:
    """校验证据链内部一致性, 返回问题列表(空 = 自洽)。

    检查项:
    - orphan_fill: 成交无对应订单;
    - fill_missing / fill_mismatch: 订单 filled_quantity 与成交明细不符;
    - buy_lot_mismatch: BUY 订单 lot「剩余 + 已卖出」≠ filled;
    - sell_alloc_mismatch: SELL 订单 Σ sell_allocation ≠ filled;
    - position_mismatch: 净成交(BUY − SELL)≠ 持仓量;
    - reconciliation_failure: 有交易但最终对账未通过;
    - soak_result_invalid: soak 验收结果非法。
    """
    orders = chain.get("orders") or []
    fills = chain.get("fills") or []
    lots = chain.get("lots") or []
    sell_allocations = chain.get("sell_allocations") or []
    position = chain.get("position") or {}
    reconciliation = chain.get("reconciliation") or {}
    soak_result = chain.get("soak_result") or {}

    issues: list[str] = []

    order_by_cid: dict[str, list[dict]] = {}
    for o in orders:
        cid = o.get("client_order_id")
        if cid:
            order_by_cid.setdefault(cid, []).append(o)

    fill_sum: dict[str, float] = {}
    for f in fills:
        cid = f.get("client_order_id")
        if cid:
            fill_sum[cid] = fill_sum.get(cid, 0.0) + float(f.get("quantity") or 0.0)

    # orphan fill
    for cid in sorted(fill_sum):
        if cid not in order_by_cid:
            issues.append(f"orphan_fill: 成交 client_order_id={cid} 无对应订单")

    buy_filled = 0.0
    sell_filled = 0.0
    for o in orders:
        cid = o.get("client_order_id")
        side = (o.get("side") or "").upper()
        filled = float(o.get("filled_quantity") or 0.0)
        if filled <= 0:
            continue
        if side == "BUY":
            buy_filled += filled
        elif side == "SELL":
            sell_filled += filled

        # fill coverage
        if cid in fill_sum:
            if abs(fill_sum[cid] - filled) > tolerance:
                issues.append(
                    f"fill_mismatch: {cid} filled={filled} vs fills={round(fill_sum[cid], 8)}"
                )
        else:
            issues.append(f"fill_missing: {cid} filled={filled} 无成交明细")

        # lot / sell allocation
        if side == "BUY":
            order_lots = [lot for lot in lots if lot.get("client_order_id") == cid]
            if not order_lots:
                issues.append(f"buy_lot_mismatch: {cid} filled={filled} 无 lot")
            else:
                total_original = 0.0
                for lot in order_lots:
                    remaining = (
                        0.0
                        if (lot.get("status") or "") == "closed"
                        else float(lot.get("quantity") or 0.0)
                    )
                    sold = sum(
                        float(a.get("quantity") or 0.0)
                        for a in sell_allocations
                        if a.get("lot_id") == lot.get("id")
                    )
                    total_original += remaining + sold
                if abs(total_original - filled) > tolerance:
                    issues.append(
                        f"buy_lot_mismatch: {cid} filled={filled} vs lot 反推={round(total_original, 8)}"
                    )
        elif side == "SELL":
            alloc_sum = sum(
                float(a.get("quantity") or 0.0)
                for a in sell_allocations
                if a.get("sell_client_order_id") == cid
            )
            if abs(alloc_sum - filled) > tolerance:
                issues.append(
                    f"sell_alloc_mismatch: {cid} filled={filled} vs alloc={round(alloc_sum, 8)}"
                )

    # fill → position
    pos_qty = float(position.get("quantity") or 0.0)
    if orders or fills:
        net = buy_filled - sell_filled
        if abs(net - pos_qty) > tolerance:
            issues.append(f"position_mismatch: 净成交 {round(net, 8)} vs 持仓 {pos_qty}")

    # reconciliation
    if orders and not (reconciliation.get("reconciled") or reconciliation.get("pass")):
        issues.append("reconciliation_failure: 有交易但最终对账未通过")

    # soak result
    verdict = soak_result.get("result")
    if verdict not in _VALID_SOAK_VERDICTS:
        issues.append(f"soak_result_invalid: 未知验收结果 {verdict!r}")

    return issues


# ---------------------------------------------------------------------------
# 从 P0-4 run_dir 读取 run 级事实
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_run_evidence(run_dir: Path | str) -> dict:
    """从 P0-4 的 logs/soak/<run_id>/ 读 metadata.json + summary.json, 提取 run 级事实。

    缺失/损坏文件保守回退空值(不崩溃), 供上层组装证据链的 run 级字段。
    """
    run_dir = Path(run_dir)
    metadata = _read_json(run_dir / "metadata.json")
    summary = _read_json(run_dir / "summary.json")
    acceptance = (summary or {}).get("acceptance") or metadata.get("acceptance_result") or {}

    return {
        "run_id": metadata.get("run_id", ""),
        "git_sha": metadata.get("git_sha", ""),
        "start_time": metadata.get("start_time", ""),
        "symbol": metadata.get("symbol", ""),
        "paper_trading": metadata.get("paper_trading"),
        "binance_testnet": metadata.get("binance_testnet"),
        "soak_result": acceptance,
    }
