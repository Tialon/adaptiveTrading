"""V11.4 长跑仿真局部夹具: 可拨动逻辑时钟(替换 time.time)。"""

import time

import pytest

from tests.long_running._harness import FakeClock


@pytest.fixture
def clock(monkeypatch):
    """全局替换 time.time 为可拨动逻辑时钟; 每个测试独立。"""
    c = FakeClock()
    monkeypatch.setattr(time, "time", c)
    return c
