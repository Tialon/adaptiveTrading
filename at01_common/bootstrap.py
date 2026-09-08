"""项目 bootstrap: 把 atXX 分层目录注入 sys.path。

run.py 等入口在 import 任何 atXX 包前先调 `inject_sys_path()`, 使
at01_common / at10_web / ... / at70_backtest 均可被 `import`。

V11.6 P2 从 run.py 顶部内联循环抽出(与 tests/conftest.py 的注入同一语义, 仅无 at80_optimizer)。
"""

from __future__ import annotations

import sys
from pathlib import Path

_LAYER_DIRS = (
    "at01_common",
    "at10_web",
    "at20_market",
    "at30_analytics",
    "at40_journal",
    "at50_strategy",
    "at50_execution",
    "at55_portfolio",
    "at60_risk",
    "at70_backtest",
)


def inject_sys_path(root: Path | None = None) -> Path:
    """把各 atXX 目录插入 sys.path(幂等), 返回项目根目录。"""
    root = root or Path(__file__).resolve().parent.parent
    for d in _LAYER_DIRS:
        p = root / d
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return root
