"""项目 bootstrap: 把 atXX 分层目录注入 sys.path。

run.py 等入口在 import 任何 atXX 包前先调 `inject_sys_path()`, 使下列各层均可被 `import`。
**编号即阅读顺序 = 数据流顺序**(L0 基础 → L9 展示), 见 docs/architecture.md。

V11.6 P2 从 run.py 顶部内联循环抽出(与 tests/conftest.py 的注入同一语义,
仅无 at85_optimizer —— 参数优化是离线研究工具, 运行时主链路不依赖)。
"""

from __future__ import annotations

import sys
from pathlib import Path

_LAYER_DIRS = (
    "at01_common",      # L0 基础(横切)
    "at10_market",      # L1 行情接入
    "at20_analytics",   # L2 分析
    "at30_strategy",    # L3 策略
    "at40_portfolio",   # L4 组合
    "at50_risk",        # L5 风控
    "at60_execution",   # L6 执行
    "at70_journal",     # L7 记录
    "at80_backtest",    # L8 研究-回测
    "at90_web",         # L9 展示(横切)
)


def inject_sys_path(root: Path | None = None) -> Path:
    """把各 atXX 目录插入 sys.path(幂等), 返回项目根目录。"""
    root = root or Path(__file__).resolve().parent.parent
    for d in _LAYER_DIRS:
        p = root / d
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    return root
