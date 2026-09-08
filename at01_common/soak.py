"""测试网无人值守 soak 运行器 + 运行时证据记录(V11.6 P1-7 / P1-8)

以子进程方式启动 `run.py`(默认测试网真实下单 `PAPER_TRADING=false` + `BINANCE_TESTNET=true`),
周期采样 `/api/metrics` 健康快照, 逐行追加到 `logs/soak-evidence.jsonl` 作为**运行时证据**。
检测进程崩溃 / KILLED / 急停触发 / can_buy 翻转, 到点(或 Ctrl+C)输出摘要并停机。

用法:
    python -m at01_common.soak --hours 7 [--paper] [--port 8800] [--interval 60] [--no-launch]

    --hours N       soak 时长(小时), 默认 7; 到点优雅停机并打印摘要
    --interval N    采样间隔(秒), 默认 60
    --port N        run.py 面板端口, 默认 8800
    --paper         纸面模式(默认 PAPER_TRADING=false 真实下单; 传 --paper 则纸面)
    --no-launch     不启动 run.py, 只对已运行实例采样记录
    --evidence PATH 证据文件路径, 默认 logs/soak-evidence.jsonl

证据文件每条一行 JSON, 字段见 `build_evidence_line`。这是运维工具(启动 + 采样 + 证据),
不改交易逻辑; 纯逻辑(build_evidence_line / classify_soak_event / summarize_soak)独立可测。

诚实边界: soak 到点走优雅停机 `POST /api/shutdown`(需 WEB_ADMIN_TOKEN)→ 等待退出 →
超时 terminate → 再超时 kill; 每一步都落 evidence, 见 `shutdown_proc`。证据逐行落盘、
账务落库是单事务、急停态持久化, 硬终止后重启安全。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# 证据文件默认位置(logs/ 已 gitignore, 属运行时数据)
DEFAULT_EVIDENCE_PATH = Path("logs") / "soak-evidence.jsonl"


# ---------------------------------------------------------------------------
# 纯逻辑(可测): 证据行 / 迁移检测 / 摘要
# ---------------------------------------------------------------------------


def build_evidence_line(ts: float, health: dict) -> dict:
    """从 `/api/metrics` 健康快照(P1-2 契约)提取一条 soak 证据行。

    只取可审计的稳定字段; 缺失字段保守回退为 False/0/"", 不因面板字段漂移而崩溃。
    """
    return {
        "ts": ts,
        "state": health.get("state") or health.get("status") or "?",
        "can_buy": bool(health.get("can_buy")),
        "can_sell": bool(health.get("can_sell")),
        "kill_armed": bool((health.get("kill_switch") or {}).get("armed")),
        "reconciled": bool((health.get("reconcile") or {}).get("reconciled")),
        "uptime_s": round(float(health.get("uptime_seconds") or 0.0), 2),
        "tasks_failure_count": int((health.get("tasks") or {}).get("failure_count") or 0),
        "buy_block_reason": health.get("buy_block_reason") or "",
        "sell_block_reason": health.get("sell_block_reason") or "",
        "last_error": health.get("last_error") or "",
    }


def classify_soak_event(prev: dict | None, cur: dict) -> str | None:
    """检测值得记录的迁移事件(状态变化 / 禁买翻转 / 急停触发); 无变化返回 None。"""
    if prev is None:
        return "start"
    events: list[str] = []
    if cur.get("state") != prev.get("state"):
        events.append(f"state {prev.get('state')}->{cur.get('state')}")
    if prev.get("can_buy") and not cur.get("can_buy"):
        events.append(f"can_buy 翻转为禁买: {cur.get('buy_block_reason') or '未知'}")
    elif not prev.get("can_buy") and cur.get("can_buy"):
        events.append("can_buy 恢复")
    if not prev.get("kill_armed") and cur.get("kill_armed"):
        events.append("急停触发")
    return "; ".join(events) if events else None


def summarize_soak(lines: list[dict]) -> dict:
    """soak 证据摘要: 样本数 / 时长 / 状态分布 / 是否曾禁买·急停 / 后台任务峰值失败。"""
    if not lines:
        return {"samples": 0}
    states: dict[str, int] = {}
    for ln in lines:
        s = str(ln.get("state") or "?")
        states[s] = states.get(s, 0) + 1
    first, last = lines[0], lines[-1]
    return {
        "samples": len(lines),
        "duration_s": round(float(last.get("ts", 0)) - float(first.get("ts", 0)), 2),
        "states": states,
        "any_can_buy_false": any(not ln.get("can_buy") for ln in lines),
        "any_kill_armed": any(ln.get("kill_armed") for ln in lines),
        "max_tasks_failure_count": max(
            int(ln.get("tasks_failure_count") or 0) for ln in lines
        ),
    }


def _classify_shutdown(shutdown_events: list[dict]) -> tuple[str, bool]:
    """从 shutdown 证据事件推导停机方式与「进程是否自行崩溃」→ (method, crashed)。"""
    if not shutdown_events:
        return ("not_launched", False)
    events = [e.get("event") for e in shutdown_events]
    exits = {e.get("event"): e.get("exit") for e in shutdown_events}
    if "already_exited" in events:
        code = exits.get("already_exited")
        crashed = code not in (0, None)
        return ("crashed" if crashed else "self_exited", crashed)
    if "graceful_exit" in events:
        return ("graceful", False)
    if "kill_timeout" in events:
        return ("kill_timeout", True)
    if "killed_exit" in events:
        return ("killed", False)
    if "terminated_exit" in events:
        return ("terminated", False)
    return ("unknown", False)


def evaluate_soak_result(
    lines: list[dict],
    *,
    requested_duration_s: float,
    shutdown_events: list[dict] | None = None,
    min_samples: int = 10,
    min_duration_ratio: float = 0.9,
) -> dict:
    """soak 验收契约(纯逻辑): 判定一次 soak 运行 PASS / FAIL / BLOCKED。

    关键原则: 「can_buy 曾经 false」不等于失败 —— DEGRADED → can_buy=false 可能是**正确安全行为**;
    是否违反「预期安全契约」才是判定依据(故只把最终状态、对账、急停、任务失败、进程崩溃视为 FAIL)。

    BLOCKED(不足以判定): 无样本 / 未跑满请求时长 / 样本不足。
    FAIL(违反安全契约): 关键任务失败 / 意外急停(unexpected KILL)/ 最终对账未通过 /
        运行期未处理异常 / 进程非正常退出。
    PASS: 跑满请求时长 + 样本充足 + 无上述违反。

    返回结构化结果(result / reason / duration_s / samples / critical_failures /
    unexpected_kill / final_state / final_reconciled / any_can_buy_false /
    final_last_error / shutdown_method)。
    """
    samples = len(lines)
    duration_s = 0.0
    critical_failures = 0
    unexpected_kill = False
    final_state = "?"
    final_reconciled = True
    any_can_buy_false = False
    final_last_error = False

    if samples > 0:
        duration_s = round(float(lines[-1].get("ts", 0)) - float(lines[0].get("ts", 0)), 2)
        critical_failures = max(int(l.get("tasks_failure_count") or 0) for l in lines)
        unexpected_kill = any(bool(l.get("kill_armed")) for l in lines) or (
            lines[-1].get("state") == "KILLED"
        )
        final_state = str(lines[-1].get("state") or "?")
        final_reconciled = bool(lines[-1].get("reconciled"))
        any_can_buy_false = any(not bool(l.get("can_buy")) for l in lines)
        final_last_error = bool((lines[-1].get("last_error") or ""))

    shutdown_method, process_crashed = _classify_shutdown(shutdown_events or [])

    reasons: list[str] = []
    if samples == 0:
        verdict = "BLOCKED"
        reasons.append("无样本(面板从未就绪, 环境/网络不可达)")
    elif duration_s < requested_duration_s * min_duration_ratio:
        verdict = "BLOCKED"
        reasons.append(
            f"实际时长 {duration_s}s 未达请求 {requested_duration_s * min_duration_ratio:.0f}s"
        )
    elif samples < min_samples:
        verdict = "BLOCKED"
        reasons.append(f"样本数 {samples} < {min_samples}, 不足以判定")
    else:
        verdict = "PASS"
        if critical_failures > 0:
            verdict = "FAIL"
            reasons.append(f"关键后台任务失败 {critical_failures} 次")
        if unexpected_kill:
            verdict = "FAIL"
            reasons.append("意外急停(unexpected KILL)")
        if not final_reconciled:
            verdict = "FAIL"
            reasons.append("最终对账未通过(reconciliation failure)")
        if final_last_error:
            verdict = "FAIL"
            reasons.append("运行期未处理异常(runtime exception)")
        if process_crashed:
            verdict = "FAIL"
            reasons.append(f"进程非正常退出(shutdown 方法: {shutdown_method})")

    return {
        "result": verdict,
        "reason": reasons,
        "duration_s": duration_s,
        "samples": samples,
        "critical_failures": critical_failures,
        "unexpected_kill": unexpected_kill,
        "final_state": final_state,
        "final_reconciled": final_reconciled,
        "any_can_buy_false": any_can_buy_false,
        "final_last_error": final_last_error,
        "shutdown_method": shutdown_method,
    }


# ---------------------------------------------------------------------------
# 运维: 采样 / 启动 / 主循环
# ---------------------------------------------------------------------------


def fetch_health(port: int) -> dict | None:
    """GET /api/metrics 返回健康快照; 面板未就绪 / 断连返回 None(不抛)。"""
    url = f"http://127.0.0.1:{port}/api/metrics"
    try:
        with urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (URLError, OSError, ValueError):
        return None


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 优雅停机(V11.7 P0-2): /api/shutdown → 等待 → terminate → kill, 全程落 evidence
# ---------------------------------------------------------------------------


def _request_shutdown(
    port: int, admin_token: str | None = None, timeout: float = 5.0
) -> bool:
    """POST /api/shutdown 请求优雅停机; 任何失败(连接/超时/非 2xx/无令牌被拒)返回 False, 不抛。"""
    try:
        req = Request(
            f"http://127.0.0.1:{port}/api/shutdown", data=b"", method="POST"
        )
        if admin_token:
            req.add_header("X-Admin-Token", admin_token)
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def shutdown_proc(
    proc: subprocess.Popen | None,
    port: int = 8800,
    admin_token: str | None = None,
    request_shutdown=None,
    graceful_timeout: float = 30.0,
    terminate_timeout: float = 15.0,
) -> list[dict]:
    """优雅停机子进程 run.py, 返回证据事件列表(每条约 `{"event": ...}`)。

    停机阶梯: POST /api/shutdown → 等待退出 → 超时 terminate → 再超时 kill。
    - shutdown 请求失败(断连/无令牌/非 2xx)→ 记 `shutdown_unavailable`, 继续 terminate, 不崩溃;
    - 进程已退出 → 记 `already_exited`, 安全返回;
    - terminate / kill 均有明确 evidence(哪个阶段、退出码);
    - 全程不抛异常(超时/失败一律转为证据事件)。

    `request_shutdown` 可注入(测试用), 默认 `_request_shutdown`。
    """
    if request_shutdown is None:
        request_shutdown = _request_shutdown

    evidence: list[dict] = []
    if proc is None:
        return evidence  # --no-launch: 无子进程可停
    if proc.poll() is not None:
        evidence.append({"event": "already_exited", "exit": proc.returncode})
        return evidence

    # 1) 优雅停机(shutdown 请求自身抛异常也不崩溃, 降级为 terminate)
    try:
        shutdown_ok = bool(request_shutdown(port, admin_token))
    except Exception:
        shutdown_ok = False
        evidence.append({"event": "shutdown_request_error"})
    if shutdown_ok:
        evidence.append({"event": "shutdown_requested"})
        try:
            proc.wait(timeout=graceful_timeout)
            evidence.append({"event": "graceful_exit", "exit": proc.returncode})
            return evidence
        except subprocess.TimeoutExpired:
            evidence.append({"event": "graceful_timeout", "seconds": graceful_timeout})
    else:
        evidence.append({"event": "shutdown_unavailable"})

    # 2) terminate
    proc.terminate()
    evidence.append({"event": "terminate_sent"})
    try:
        proc.wait(timeout=terminate_timeout)
        evidence.append({"event": "terminated_exit", "exit": proc.returncode})
        return evidence
    except subprocess.TimeoutExpired:
        evidence.append({"event": "terminate_timeout", "seconds": terminate_timeout})

    # 3) kill
    proc.kill()
    evidence.append({"event": "kill_sent"})
    try:
        proc.wait(timeout=terminate_timeout)
        evidence.append({"event": "killed_exit", "exit": proc.returncode})
    except subprocess.TimeoutExpired:
        evidence.append({"event": "kill_timeout", "seconds": terminate_timeout})
    return evidence


def main(argv: list[str] | None = None) -> int:
    """soak 运行器入口。返回进程退出码(0 正常, 1 用法错误)。"""
    args = (argv if argv is not None else sys.argv[1:])
    hours = 7.0
    interval = 60.0
    port = 8800
    paper = False
    no_launch = False
    evidence_path = DEFAULT_EVIDENCE_PATH

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--hours" and i + 1 < len(args):
            hours = float(args[i + 1]); i += 2
        elif a == "--interval" and i + 1 < len(args):
            interval = float(args[i + 1]); i += 2
        elif a == "--port" and i + 1 < len(args):
            port = int(args[i + 1]); i += 2
        elif a == "--paper":
            paper = True; i += 1
        elif a == "--no-launch":
            no_launch = True; i += 1
        elif a == "--evidence" and i + 1 < len(args):
            evidence_path = Path(args[i + 1]); i += 2
        else:
            print(f"未知参数: {a}", file=sys.stderr)
            return 1

    env = dict(os.environ)
    env["PAPER_TRADING"] = "true" if paper else "false"
    env["BINANCE_TESTNET"] = "true"

    proc: subprocess.Popen | None = None
    if not no_launch:
        proc = subprocess.Popen(
            [sys.executable, "run.py"],
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

    deadline = time.time() + hours * 3600.0
    lines: list[dict] = []
    prev: dict | None = None
    try:
        while time.time() < deadline:
            health = fetch_health(port)
            if health is None:
                print(f"[{time.strftime('%H:%M:%S')}] 面板未就绪/断连, 重试中...")
                if proc is not None and proc.poll() is not None:
                    print(f"run.py 进程已退出(exit={proc.returncode}), 停止 soak")
                    break
            else:
                line = build_evidence_line(time.time(), health)
                _append_jsonl(evidence_path, line)
                lines.append(line)
                event = classify_soak_event(prev, line)
                if event:
                    print(f"[{time.strftime('%H:%M:%S')}] {event} | state={line['state']} "
                          f"can_buy={line['can_buy']} reconciled={line['reconciled']}")
                prev = line
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n收到中断, 停止 soak")
    finally:
        # V11.7 P0-2: 优雅停机(/api/shutdown → terminate → kill), 全程落证据
        shutdown_events = shutdown_proc(
            proc, port=port, admin_token=(os.environ.get("WEB_ADMIN_TOKEN") or None)
        )
        for ev in shutdown_events:
            _append_jsonl(evidence_path, {"ts": time.time(), **ev})
            print(f"[{time.strftime('%H:%M:%S')}] shutdown: {ev.get('event')}")

    summary = summarize_soak(lines)
    summary["shutdown"] = shutdown_events
    print("=== soak 证据摘要 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"证据文件: {evidence_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
