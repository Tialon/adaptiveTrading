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

诚实边界: soak 停机走 `subprocess.terminate()`(硬终止, 非 run.py 的 Ctrl+C 优雅停机)—— 证据已
逐行落盘、账务落库是单事务、急停态持久化, 硬终止后重启安全。真正优雅停机请用
`POST /api/shutdown`(需 WEB_ADMIN_TOKEN)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

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
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    summary = summarize_soak(lines)
    print("=== soak 证据摘要 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"证据文件: {evidence_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
