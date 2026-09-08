"""V11.6 P2: run.py 轻量抽取(bootstrap/runtime/wiring)回归。

验证抽取后:
1. `at01_common.bootstrap.inject_sys_path` 幂等注入全部 atXX 分层目录;
2. `run.AdaptiveTradingSystem` 交易语义方法仍完整在位(未因抽取丢失);
3. `wire_system` / `runtime.run` 为可调用协程(抽取出的装配与运行时入口可导入、可调用)。

编排粘合(装配/生命周期/信号)依赖真实引擎 + 网络, 本测试只锁「结构不回归」, 不跑真实装配;
真实装配行为由既有集成测试 + 测试网运维手册覆盖。
"""

import inspect
import sys
from pathlib import Path

import run
from at01_common import bootstrap
from at01_common import runtime as runtime_mod
from at01_common import wiring as wiring_mod


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def test_inject_sys_path_adds_all_layer_dirs():
    root = bootstrap.inject_sys_path()
    for d in bootstrap._LAYER_DIRS:
        assert str(root / d) in sys.path


def test_inject_sys_path_idempotent():
    bootstrap.inject_sys_path()
    before = len(sys.path)
    bootstrap.inject_sys_path()
    assert len(sys.path) == before, "重复注入不应向 sys.path 追加重复条目"


def test_inject_sys_path_returns_project_root():
    root = bootstrap.inject_sys_path()
    assert (root / "at01_common").is_dir()
    assert (root / "run.py").is_file()


def test_system_keeps_trading_semantics_methods():
    # 抽取 bootstrap/wiring/runtime 后, 交易语义方法必须原样留在 AdaptiveTradingSystem 内
    expected = [
        "_on_signal",
        "_risk_loop",
        "_reconcile_loop",
        "_apply_verdict",
        "_apply_breaker_decision",
        "_record_breaker_decision",
        "_apply_core_action",
        "_compute_fund_drift",
        "_runtime_health",
        "_persist_critical_kill_switch",
        "initialize",
        "start",
        "stop",
    ]
    for name in expected:
        assert callable(getattr(run.AdaptiveTradingSystem, name)), name


def test_initialize_delegates_to_wire_system():
    # initialize 保留在类上, 且仍是协程(内部委托给 wiring.wire_system)
    assert inspect.iscoroutinefunction(run.AdaptiveTradingSystem.initialize)


def test_wire_system_and_run_are_coroutines():
    assert inspect.iscoroutinefunction(wiring_mod.wire_system)
    assert inspect.iscoroutinefunction(runtime_mod.run)
