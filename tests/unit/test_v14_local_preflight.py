"""V14 §4.1 Level 1 启动安全预检测试。

**这里钉死的是一个实测踩到的真问题**: 运行参数优先级是 **DB > env**,
所以一个老开发库里的 `runtime_config` 覆盖会**盖过** `start-local.ps1` 设的环境变量。
仓库根的 `adaptive.db` 里残留着早先模式切换测试写入的
`TRADING_MODE=live` + `LIVE_TRADING_CONFIRM=true` + `MAINNET_API_SCOPE_CONFIRM=true`,
于是「本机开发启动」直接把系统带到了**主网**上。

Level 1 是开发环境, 绝不允许因为一个库而连上主网 —— 本文件保证预检真的拦得住。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.local_preflight import live_hazards, main, read_overrides

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "local_preflight.py"


def _make_db(path: Path, rows: dict[str, str]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO runtime_config VALUES (?, ?)", list(rows.items()))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rows,expected", [
    ({}, 0),
    ({"TRADING_MODE": "paper", "PAPER_TRADING": "true"}, 0),
    ({"TRADING_MODE": "testnet", "BINANCE_TESTNET": "true"}, 0),
    ({"TRADING_MODE": "live"}, 1),
    ({"LIVE_TRADING_CONFIRM": "true"}, 1),
    ({"MAINNET_API_SCOPE_CONFIRM": "true"}, 1),
    ({"TRADING_MODE": "live", "LIVE_TRADING_CONFIRM": "true",
      "MAINNET_API_SCOPE_CONFIRM": "true"}, 3),
])
def test_live_hazards(rows: dict[str, str], expected: int) -> None:
    assert len(live_hazards(rows)) == expected


def test_hazard_detection_is_case_insensitive() -> None:
    """配置值大小写不该决定安全判定 —— `LIVE_TRADING_CONFIRM=TRUE` 一样危险。"""
    assert live_hazards({"LIVE_TRADING_CONFIRM": "TRUE"})
    assert live_hazards({"TRADING_MODE": "LIVE"})


def test_testnet_is_not_a_hazard() -> None:
    """测试网是假钱 —— 不该被拦(否则 Level 1 就没法验证测试网路径了)。"""
    assert live_hazards({"TRADING_MODE": "testnet", "BINANCE_TESTNET": "true"}) == []


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def test_read_overrides_from_a_real_sqlite_file(tmp_path) -> None:
    db = tmp_path / "x.db"
    _make_db(db, {"TRADING_MODE": "live", "FOO": "bar"})
    assert read_overrides(db) == {"TRADING_MODE": "live", "FOO": "bar"}


def test_missing_file_is_not_an_error(tmp_path) -> None:
    """库不存在 = 全新开发库 = 安全。预检失败不该阻断正常开发。"""
    assert read_overrides(tmp_path / "nope.db") == {}


def test_non_sqlite_file_is_not_an_error(tmp_path) -> None:
    """指向了非 SQLite 的文件(或空文件)时也不该抛异常。"""
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database", encoding="utf-8")
    assert read_overrides(junk) == {}


def test_db_without_runtime_config_table_is_safe(tmp_path) -> None:
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    assert read_overrides(db) == {}
    assert live_hazards(read_overrides(db)) == []


# ---------------------------------------------------------------------------
# 退出码(脚本按它决定是否放行)
# ---------------------------------------------------------------------------


def test_main_exits_zero_for_a_clean_db(tmp_path, capsys) -> None:
    db = tmp_path / "clean.db"
    _make_db(db, {"TRADING_MODE": "paper"})
    assert main(["local_preflight.py", str(db)]) == 0


def test_main_exits_two_for_a_live_db(tmp_path, capsys) -> None:
    db = tmp_path / "danger.db"
    _make_db(db, {"TRADING_MODE": "live", "LIVE_TRADING_CONFIRM": "true",
                  "MAINNET_API_SCOPE_CONFIRM": "true"})
    assert main(["local_preflight.py", str(db)]) == 2
    out = capsys.readouterr().out
    assert "DANGER=" in out
    assert "实盘主网" in out
    # 必须给出可执行的补救方式, 不能只说「不行」
    assert "模拟" in out and "-DbFile" in out


def test_override_summary_is_printed_even_when_safe(tmp_path, capsys) -> None:
    """有覆盖但安全时也要打印 —— 用户有权知道这次启动的模式是被库决定的。"""
    db = tmp_path / "t.db"
    _make_db(db, {"TRADING_MODE": "testnet"})
    assert main(["local_preflight.py", str(db)]) == 0
    assert "OVERRIDES=" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 真跑一次(端到端, 与 PowerShell 调用方式一致)
# ---------------------------------------------------------------------------


def test_script_runs_standalone_and_reports_exit_code(tmp_path) -> None:
    db = tmp_path / "danger.db"
    _make_db(db, {"TRADING_MODE": "live", "LIVE_TRADING_CONFIRM": "true"})
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(db)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "DANGER=" in proc.stdout


def test_start_local_script_defaults_to_a_separate_dev_db() -> None:
    """默认库必须**不是**仓库根的 adaptive.db —— 那是历史状态的聚集地。

    回归: 默认指向 `data\\local-dev.db` 才能让 Level 1 有一个可丢弃的干净起点。
    """
    text = (REPO_ROOT / "scripts" / "start-local.ps1").read_text(encoding="utf-8-sig")
    assert '$DbFile = "data\\local-dev.db"' in text
    assert "local_preflight.py" in text, "必须调用预检, 否则老库会静默把开发带进实盘"


def test_start_local_script_is_utf8_bom() -> None:
    """Windows PowerShell 5.1 按 ANSI 读取无 BOM 的 .ps1 —— 中文会被拆坏导致语法错误。

    实测踩到过: 无 BOM 时脚本直接 `Missing closing ')'`。BOM 是 5.1 与 7 都认的写法。
    """
    raw = (REPO_ROOT / "scripts" / "start-local.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "缺少 UTF-8 BOM, PowerShell 5.1 会读坏中文"
