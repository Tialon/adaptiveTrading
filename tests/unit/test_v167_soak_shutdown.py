"""V11.7 P0-2 证明: soak 优雅停机阶梯(/api/shutdown → terminate → kill)。

覆盖 `at01_common/soak.py` 的 `shutdown_proc` / `_request_shutdown`:
- graceful shutdown: /api/shutdown 成功 → 进程优雅退出(exit 0), 不 terminate/kill;
- shutdown endpoint unavailable: 请求失败 → 降级 terminate → 退出, 不崩溃;
- process already exited: 安全返回 `already_exited`, 不再 terminate/kill;
- graceful timeout: shutdown 后进程未在宽限期内退出 → terminate;
- terminate fallback / kill fallback: terminate 超时 → kill;
- repeated shutdown: 幂等(第二次 already_exited);
- Ctrl+C: 主循环 finally 走同一 shutdown_proc(进程仍在时照常优雅停机);
- shutdown 请求自身抛异常: 降级 terminate, 不崩溃。

fake Popen(鸭子类型)不依赖真实 run.py 进程, 纯同步可测。
"""

import subprocess

from at01_common import soak
from at01_common.soak import _request_shutdown, shutdown_proc


class FakeProc:
    """可控 Popen 桩: poll/wait/terminate/kill 行为按配置驱动。"""

    def __init__(self, *, alive=True, exit_on_terminate=True, exit_on_kill=True):
        self._alive = alive
        self._exit_on_terminate = exit_on_terminate
        self._exit_on_kill = exit_on_kill
        self.returncode = None
        self.terminated = False
        self.killed = False

    def _set_exited(self, code):
        self._alive = False
        self.returncode = code

    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired("run.py", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        if self._exit_on_terminate:
            self._alive = False

    def kill(self):
        self.killed = True
        self.returncode = -9
        if self._exit_on_kill:
            self._alive = False


def _events(evs):
    return [e["event"] for e in evs]


# ---------------------------------------------------------------------------
# shutdown_proc
# ---------------------------------------------------------------------------


def test_graceful_shutdown():
    proc = FakeProc(alive=True)

    def req(port, token=None):
        proc._set_exited(0)  # run.py 优雅停机成功退出
        return True

    evs = shutdown_proc(proc, request_shutdown=req)
    assert _events(evs) == ["shutdown_requested", "graceful_exit"]
    assert evs[-1]["exit"] == 0
    assert not proc.terminated and not proc.killed


def test_shutdown_unavailable_falls_back_to_terminate():
    proc = FakeProc(alive=True)
    evs = shutdown_proc(proc, request_shutdown=lambda *a: False)
    assert _events(evs) == ["shutdown_unavailable", "terminate_sent", "terminated_exit"]
    assert evs[-1]["exit"] == -15
    assert proc.terminated and not proc.killed


def test_process_already_exited():
    proc = FakeProc(alive=False)
    proc.returncode = 3
    evs = shutdown_proc(proc, request_shutdown=lambda *a: True)
    assert _events(evs) == ["already_exited"]
    assert evs[-1]["exit"] == 3
    assert not proc.terminated and not proc.killed


def test_graceful_timeout_then_terminate():
    proc = FakeProc(alive=True)  # shutdown 后仍不退出(宽限期超时)
    evs = shutdown_proc(proc, request_shutdown=lambda *a: True, graceful_timeout=0.01)
    assert _events(evs) == [
        "shutdown_requested",
        "graceful_timeout",
        "terminate_sent",
        "terminated_exit",
    ]
    assert proc.terminated and not proc.killed


def test_kill_fallback():
    proc = FakeProc(alive=True, exit_on_terminate=False)  # terminate 也超时 → kill
    evs = shutdown_proc(
        proc,
        request_shutdown=lambda *a: True,
        graceful_timeout=0.01,
        terminate_timeout=0.01,
    )
    assert "kill_sent" in _events(evs)
    assert "killed_exit" in _events(evs)
    assert evs[-1]["exit"] == -9
    assert proc.terminated and proc.killed


def test_repeated_shutdown_is_idempotent():
    proc = FakeProc(alive=True)
    first = shutdown_proc(proc, request_shutdown=lambda *a: False)
    assert "terminated_exit" in _events(first)
    second = shutdown_proc(proc, request_shutdown=lambda *a: True)
    assert _events(second) == ["already_exited"]


def test_keyboard_interrupt_path_uses_graceful_shutdown():
    """Ctrl+C → main() finally → shutdown_proc(进程仍在) → 优雅停机。"""
    proc = FakeProc(alive=True)

    def req(port, token=None):
        proc._set_exited(0)
        return True

    evs = shutdown_proc(proc, request_shutdown=req)
    assert "shutdown_requested" in _events(evs)
    assert not proc.terminated and not proc.killed


def test_shutdown_request_raises_is_safe():
    """shutdown 请求自身抛异常 → 降级 terminate, 不崩溃。"""

    def boom(port, token=None):
        raise RuntimeError("boom")

    proc = FakeProc(alive=True)
    evs = shutdown_proc(proc, request_shutdown=boom)
    assert "shutdown_request_error" in _events(evs)
    assert "terminate_sent" in _events(evs)
    assert proc.terminated


def test_no_proc_returns_empty():
    assert shutdown_proc(None, request_shutdown=lambda *a: True) == []


# ---------------------------------------------------------------------------
# _request_shutdown(HTTP 细节)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_shutdown_success_and_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["token"] = req.get_header("X-admin-token")
        return _FakeResp(200)

    monkeypatch.setattr(soak, "urlopen", fake_urlopen)
    assert _request_shutdown(8800, admin_token="sekret") is True
    assert captured["url"] == "http://127.0.0.1:8800/api/shutdown"
    assert captured["token"] == "sekret"


def test_request_shutdown_non_2xx_returns_false(monkeypatch):
    monkeypatch.setattr(soak, "urlopen", lambda req, timeout=None: _FakeResp(503))
    assert _request_shutdown(8800, admin_token="x") is False


def test_request_shutdown_exception_returns_false(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("conn refused")

    monkeypatch.setattr(soak, "urlopen", boom)
    assert _request_shutdown(8800) is False
