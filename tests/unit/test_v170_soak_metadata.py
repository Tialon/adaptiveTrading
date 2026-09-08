"""V11.7 P0-4 证明: soak 运行的可复现元数据(run_id / git_sha / 起止时间 / 配置 / 验收结果)。

覆盖 `at01_common/soak.py` 的:
- `git_sha`: 从真实 `git rev-parse HEAD` 取代码版本(不硬编码), git 失败返回空串;
- `make_run_id`: `<UTC 时间戳>-<8 位随机 hex>` 唯一标识一次运行;
- `_iso`: UTC ISO-8601 时间;
- `build_metadata`: 把一次运行的可复现元数据组装为单一 dict(纯函数, 含舍入);
- `_write_json`: 建目录 + 写 UTF-8 JSON(供 metadata.json / summary.json 落盘)。
"""

import json
import re

from at01_common import soak
from at01_common.soak import _iso, _write_json, build_metadata, git_sha, make_run_id


# ---------------------------------------------------------------------------
# git_sha
# ---------------------------------------------------------------------------


def test_git_sha_returns_head(monkeypatch):
    class _R:
        returncode = 0
        stdout = "a" * 40 + "\n"

    monkeypatch.setattr(soak.subprocess, "run", lambda *a, **k: _R())
    assert git_sha() == "a" * 40


def test_git_sha_empty_on_failure(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(soak.subprocess, "run", lambda *a, **k: _R())
    assert git_sha() == ""


def test_git_sha_smoke_real_repo():
    """本仓库是 git 仓库, 真实 git_sha() 应返回 40 位 hex(证明 cwd 解析正确)。"""
    sha = git_sha()
    assert re.match(r"^[0-9a-f]{40}$", sha), f"git_sha() = {sha!r}"


# ---------------------------------------------------------------------------
# make_run_id
# ---------------------------------------------------------------------------


def test_make_run_id_format():
    rid = make_run_id(1700000000.0)  # 2023-11-14 22:13:20 UTC
    assert re.match(r"^\d{8}T\d{6}-[0-9a-f]{8}$", rid)
    assert rid.startswith("20231114T221320")


def test_make_run_id_unique():
    assert make_run_id() != make_run_id()


# ---------------------------------------------------------------------------
# _iso / build_metadata
# ---------------------------------------------------------------------------


def test_iso_utc():
    assert _iso(1700000000.0) == "2023-11-14T22:13:20+00:00"


def test_build_metadata_fields_and_rounding():
    acceptance = {"result": "PASS", "reason": []}
    m = build_metadata(
        run_id="20260908T134230-abc12345",
        git_sha="0" * 40,
        start_time=1700000000.0,
        end_time=1700003600.0,
        requested_duration_s=3600.0,
        actual_duration_s=3600.0,
        symbol="SOLUSDT",
        paper_trading=True,
        binance_testnet=True,
        final_state="TRADING",
        acceptance_result=acceptance,
    )
    assert m["run_id"] == "20260908T134230-abc12345"
    assert m["git_sha"] == "0" * 40
    assert m["start_time"] == "2023-11-14T22:13:20+00:00"
    assert m["end_time"] == "2023-11-14T23:13:20+00:00"
    assert m["requested_duration_s"] == 3600.0
    assert m["actual_duration_s"] == 3600.0
    assert m["symbol"] == "SOLUSDT"
    assert m["paper_trading"] is True
    assert m["binance_testnet"] is True
    assert m["final_state"] == "TRADING"
    assert m["acceptance_result"] is acceptance


def test_build_metadata_rounds_durations():
    m = build_metadata(
        run_id="x-y",
        git_sha="0" * 40,
        start_time=0.0,
        end_time=1.0,
        requested_duration_s=3600.1234,
        actual_duration_s=0.9876,
        symbol="SOLUSDT",
        paper_trading=False,
        binance_testnet=True,
        final_state="?",
        acceptance_result={"result": "BLOCKED"},
    )
    assert m["requested_duration_s"] == 3600.12
    assert m["actual_duration_s"] == 0.99


# ---------------------------------------------------------------------------
# _write_json
# ---------------------------------------------------------------------------


def test_write_json_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deep" / "metadata.json"
    _write_json(p, {"a": 1, "中文": "值"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "中文": "值"}
