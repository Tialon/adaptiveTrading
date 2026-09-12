"""Level 1 本机启动安全预检(V14 §4.1)。

**这个脚本解决什么**: 运行参数的优先级是 **DB > env > default**。所以一个老开发库里的
`runtime_config` 覆盖会**盖过** `start-local.ps1` 设的环境变量 —— 也就是说,
「本机开发启动」可能被一个历史残留直接带进**主网**。

实测踩到过: 仓库根的 `adaptive.db` 里残留着早先模式切换测试写入的
`TRADING_MODE=live` + `LIVE_TRADING_CONFIRM=true` + `MAINNET_API_SCOPE_CONFIRM=true`,
`start-local.ps1` 一跑, 系统直奔主网(靠 `live_equity` 播种失败才没构成真实下单)。

Level 1 的定位是「开发方便」, 因此**绝不允许**因为一个库而连上主网。
本脚本在启动前把这件事查清楚。

退出码:
    0   可以启动(无覆盖, 或覆盖不含实盘)
    2   **拒绝启动** —— 该库会让本机开发进入实盘主网
    0   读不到库(不存在/非 SQLite)也返回 0 —— 预检失败不该阻断正常开发

用法:
    python scripts/local_preflight.py data/local-dev.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# 出现即判定「会进入实盘主网」的覆盖组合
_LIVE_MARKERS: tuple[tuple[str, str], ...] = (
    ("TRADING_MODE", "live"),
    ("LIVE_TRADING_CONFIRM", "true"),
    ("MAINNET_API_SCOPE_CONFIRM", "true"),
)


def read_overrides(db_path: str | Path) -> dict[str, str]:
    """读 `runtime_config`(表不存在/不是 SQLite 一律返回空 —— 查不到就当没有)。"""
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute("SELECT key, value FROM runtime_config").fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    return {str(k): str(v) for k, v in rows}


def live_hazards(overrides: dict[str, str]) -> list[str]:
    """返回会让本机开发进入实盘的覆盖项(纯函数, 便于测试)。"""
    out: list[str] = []
    for key, expected in _LIVE_MARKERS:
        if str(overrides.get(key, "")).strip().lower() == expected:
            out.append(f"{key}={overrides[key]}")
    return out


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else "data/local-dev.db"
    overrides = read_overrides(db_path)
    hazards = live_hazards(overrides)

    if overrides:
        print("OVERRIDES=" + ",".join(f"{k}={v}" for k, v in sorted(overrides.items())))
    if hazards:
        print("DANGER=" + ",".join(hazards))
        print("")
        print("拒绝启动: 该库会让本机开发进入【实盘主网】")
        print("  Level 1 是开发环境, 不应连主网。三种处理方式:")
        print("    1) 用默认的干净开发库:  直接跑 start-local.ps1(不带 -DbFile)")
        print("    2) 换一个库:            -DbFile data\\other.db")
        print("    3) 继续用这个库:        先在 /admin 页面把模式切回「模拟」")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
