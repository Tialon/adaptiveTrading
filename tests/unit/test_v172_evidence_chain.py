"""V11.7 P1-3 证明: Testnet execution evidence chain(证据链组装 + 一致性校验)。

覆盖 `at01_common/evidence_chain.py`:
- `build_evidence_chain`: 组装 + answers 汇总(订单数/成交/手续费/持仓/对账/soak);
- `chain_consistency_issues`: 校验链路自洽, 覆盖 orphan_fill / fill_missing /
  fill_mismatch / buy_lot_mismatch / sell_alloc_mismatch / position_mismatch /
  reconciliation_failure / soak_result_invalid;
- `load_run_evidence`: 从 P0-4 run_dir 读 metadata/summary 提取 run 级事实。
"""

import json

from at01_common.evidence_chain import (
    build_evidence_chain,
    chain_consistency_issues,
    load_run_evidence,
)


# ---------------------------------------------------------------------------
# 构造工具(订单/成交/lot/卖出分配/持仓)
# ---------------------------------------------------------------------------


def _order(cid, side, filled, qty=None):
    return {
        "client_order_id": cid,
        "symbol": "SOLUSDT",
        "side": side,
        "quantity": qty if qty is not None else filled,
        "filled_quantity": filled,
        "status": "FILLED",
        "is_paper": True,
    }


def _fill(cid, side, qty, fee=0.001):
    return {
        "client_order_id": cid,
        "symbol": "SOLUSDT",
        "side": side,
        "quantity": qty,
        "quote_quantity": qty * 100.0,
        "fee_quote": fee,
        "commission": fee,
        "trade_time": 1700000000000,
    }


def _lot(lot_id, cid, remaining=0.0, status="closed"):
    return {"id": lot_id, "client_order_id": cid, "quantity": remaining, "status": status}


def _sell_alloc(sell_cid, lot_id, qty):
    return {"sell_client_order_id": sell_cid, "lot_id": lot_id, "quantity": qty}


def _chain(**overrides):
    base = {
        "run_id": "run-1",
        "git_sha": "0" * 40,
        "start_time": "2026-09-08T00:00:00+00:00",
        "symbol": "SOLUSDT",
        "paper_trading": True,
        "binance_testnet": True,
        "orders": [],
        "fills": [],
        "position": {"quantity": 0.0, "avg_price": 0.0, "realized_pnl": 0.0},
        "lots": [],
        "sell_allocations": [],
        "exchange_truth": {},
        "reconciliation": {"reconciled": True},
        "soak_result": {"result": "PASS"},
    }
    base.update(overrides)
    return build_evidence_chain(**base)


# ---------------------------------------------------------------------------
# build_evidence_chain: 组装 + answers
# ---------------------------------------------------------------------------


def test_build_answers_round_trip():
    """一轮 BUY 1.0 + SELL 1.0(清仓)的完整链路, answers 汇总正确。"""
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0), _order("sell-1", "SELL", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0), _fill("sell-1", "SELL", 1.0)],
        lots=[_lot(1, "buy-1", remaining=0.0, status="closed")],
        sell_allocations=[_sell_alloc("sell-1", 1, 1.0)],
        position={"quantity": 0.0},
    )
    a = chain["answers"]
    assert a["order_count"] == 2
    assert a["fill_count"] == 2
    assert a["filled_quantity"] == 2.0
    assert a["total_fee_quote"] == 0.002
    assert a["position_quantity"] == 0.0
    assert a["reconciliation_pass"] is True
    assert a["soak_verdict"] == "PASS"
    assert chain["mode"] == {"paper_trading": True, "binance_testnet": True}


def test_build_defaults_missing_optional():
    chain = _chain()  # 空订单/成交/持仓, 缺 exchange_truth 等
    assert chain["exchange_truth"] == {}
    assert chain["answers"]["soak_verdict"] == "PASS"


# ---------------------------------------------------------------------------
# chain_consistency_issues: 一致性
# ---------------------------------------------------------------------------


def test_consistent_round_trip_has_no_issues():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0), _order("sell-1", "SELL", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0), _fill("sell-1", "SELL", 1.0)],
        lots=[_lot(1, "buy-1", remaining=0.0, status="closed")],
        sell_allocations=[_sell_alloc("sell-1", 1, 1.0)],
        position={"quantity": 0.0},
    )
    assert chain_consistency_issues(chain) == []


def test_consistent_open_position_has_no_issues():
    """只买未卖: BUY 1.0, lot 剩余 1.0, 持仓 1.0。"""
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0)],
        lots=[_lot(1, "buy-1", remaining=1.0, status="open")],
        sell_allocations=[],
        position={"quantity": 1.0},
    )
    assert chain_consistency_issues(chain) == []


def test_orphan_fill():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0), _fill("ghost", "BUY", 0.5)],
        lots=[_lot(1, "buy-1", remaining=1.0, status="open")],
        position={"quantity": 1.5},
    )
    issues = chain_consistency_issues(chain)
    assert any("orphan_fill" in i for i in issues)


def test_fill_mismatch():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[_fill("buy-1", "BUY", 0.5)],
        lots=[_lot(1, "buy-1", remaining=0.5, status="open")],
        position={"quantity": 0.5},
    )
    issues = chain_consistency_issues(chain)
    assert any("fill_mismatch" in i for i in issues)


def test_fill_missing():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[],
        lots=[_lot(1, "buy-1", remaining=1.0, status="open")],
        position={"quantity": 1.0},
    )
    issues = chain_consistency_issues(chain)
    assert any("fill_missing" in i for i in issues)


def test_buy_lot_mismatch_no_lot():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0)],
        lots=[],
        position={"quantity": 1.0},
    )
    issues = chain_consistency_issues(chain)
    assert any("buy_lot_mismatch" in i and "无 lot" in i for i in issues)


def test_buy_lot_mismatch_partial_sold():
    """lot 剩余 0.5 + 卖出 0.25 = 0.75 ≠ filled 1.0 → 反推不一致。"""
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0), _order("sell-1", "SELL", 0.25)],
        fills=[_fill("buy-1", "BUY", 1.0), _fill("sell-1", "SELL", 0.25)],
        lots=[_lot(1, "buy-1", remaining=0.5, status="open")],
        sell_allocations=[_sell_alloc("sell-1", 1, 0.25)],
        position={"quantity": 0.75},
    )
    issues = chain_consistency_issues(chain)
    assert any("buy_lot_mismatch" in i for i in issues)


def test_sell_alloc_mismatch():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0), _order("sell-1", "SELL", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0), _fill("sell-1", "SELL", 1.0)],
        lots=[_lot(1, "buy-1", remaining=0.0, status="closed")],
        sell_allocations=[_sell_alloc("sell-1", 1, 0.5)],  # 只分配 0.5
        position={"quantity": 0.0},
    )
    issues = chain_consistency_issues(chain)
    assert any("sell_alloc_mismatch" in i for i in issues)


def test_position_mismatch():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0)],
        lots=[_lot(1, "buy-1", remaining=1.0, status="open")],
        position={"quantity": 2.0},  # 持仓与净成交不符
    )
    issues = chain_consistency_issues(chain)
    assert any("position_mismatch" in i for i in issues)


def test_reconciliation_failure():
    chain = _chain(
        orders=[_order("buy-1", "BUY", 1.0)],
        fills=[_fill("buy-1", "BUY", 1.0)],
        lots=[_lot(1, "buy-1", remaining=1.0, status="open")],
        position={"quantity": 1.0},
        reconciliation={"reconciled": False},
    )
    issues = chain_consistency_issues(chain)
    assert any("reconciliation_failure" in i for i in issues)


def test_soak_result_invalid():
    chain = _chain(soak_result={"result": "MAYBE"})
    issues = chain_consistency_issues(chain)
    assert any("soak_result_invalid" in i for i in issues)


def test_not_executed_verdict_is_valid():
    """soak 未执行(NOT_EXECUTED)是合法状态, 不报 soak_result_invalid。"""
    chain = _chain(soak_result={"result": "NOT_EXECUTED"})
    assert all("soak_result_invalid" not in i for i in chain_consistency_issues(chain))


# ---------------------------------------------------------------------------
# load_run_evidence
# ---------------------------------------------------------------------------


def test_load_run_evidence_reads_metadata_and_summary(tmp_path):
    run_dir = tmp_path / "logs" / "soak" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps({
            "run_id": "run-1",
            "git_sha": "a" * 40,
            "start_time": "2026-09-08T00:00:00+00:00",
            "symbol": "SOLUSDT",
            "paper_trading": True,
            "binance_testnet": True,
        }),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps({"acceptance": {"result": "PASS", "reason": []}}),
        encoding="utf-8",
    )

    facts = load_run_evidence(run_dir)
    assert facts["run_id"] == "run-1"
    assert facts["git_sha"] == "a" * 40
    assert facts["symbol"] == "SOLUSDT"
    assert facts["paper_trading"] is True
    assert facts["soak_result"]["result"] == "PASS"


def test_load_run_evidence_missing_files_returns_empty(tmp_path):
    facts = load_run_evidence(tmp_path / "nonexistent")
    assert facts["run_id"] == ""
    assert facts["soak_result"] == {}
