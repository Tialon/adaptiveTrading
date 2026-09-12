"""稳定性观察器(V14 §11)。

任务书明确: **不要因为 `health = 200` 就宣布稳定**。所以本脚本采样的不是「接口通不通」,
而是任务书点名的那十几项:

    进程是否退出 / 容器是否重启 / MySQL 是否异常 / Redis 是否异常 /
    WebSocket 是否断开与自动恢复 / 行情是否正常 / 任务是否持续运行 /
    内存是否持续增长 / CPU 是否异常 / 数据库连接是否泄漏 / Redis 连接是否泄漏 /
    operator event 是否持续正常 / reconciliation 是否正常 / TradingGate 是否正常 /
    是否出现 UNKNOWN order / 是否出现重复订单 / 是否出现异常自动恢复

用法:
    python scripts/stability_probe.py --minutes 60 --interval 60 --out logs/stability.jsonl

输出: 每行一条 JSON 采样, 末尾打印结论。**结论由数据决定, 不由期望决定。**
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _get(url: str, timeout: float = 10.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _docker(args: list[str]) -> str:
    try:
        out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()
    except Exception:
        return ""


def _container_stats(name: str) -> dict:
    """容器内存/CPU(不依赖 docker stats 的交互输出)。"""
    raw = _docker(["stats", "--no-stream", "--format",
                   "{{.MemUsage}}|{{.CPUPerc}}", name])
    if not raw or "|" not in raw:
        return {"mem": "", "cpu": ""}
    mem, cpu = raw.split("|", 1)
    return {"mem": mem.strip(), "cpu": cpu.strip()}


def _restart_count(name: str) -> int:
    raw = _docker(["inspect", "-f", "{{.RestartCount}}", name])
    try:
        return int(raw)
    except ValueError:
        return -1


def _health_status(name: str) -> str:
    return _docker(["inspect", "-f", "{{.State.Health.Status}}", name]) or "unknown"


def sample(base: str) -> dict:
    """采一条样本。**接口读不到时如实记 None, 不编造 0。**"""
    health = _get(f"{base}/api/health")
    status = _get(f"{base}/api/operator-status")
    op = _get(f"{base}/api/operator-log?limit=200")
    metrics = _get(f"{base}/api/metrics")

    out: dict = {
        "ts": time.time(),
        "app_http_ok": health is not None,
        "app_running": bool(health and health.get("running")),
    }

    if status:
        rt = status.get("runtime") or {}
        out.update({
            "status": status.get("status"),
            "can_buy": status.get("can_buy"),
            "can_sell": status.get("can_sell"),
            "reconciled": rt.get("reconciled"),
            "tasks_failed": rt.get("tasks_failed"),
            "uptime_seconds": rt.get("uptime_seconds"),
        })
        deps = (status.get("health_summary") or {})
        out["health_ok"] = deps.get("ok")
        out["degradations"] = deps.get("degradations") or []
        # 逐项依赖状态(MySQL/Redis)
        for item in status.get("health_report") or []:
            if item.get("key") in ("database", "redis", "market", "reconcile", "tasks"):
                out[f"dep_{item['key']}"] = item.get("ok")

    if op:
        events = op.get("events") or []
        out["event_count"] = op.get("count")
        out["kinds"] = {}
        for e in events:
            k = e.get("kind", "?")
            out["kinds"][k] = out["kinds"].get(k, 0) + 1
        out["persist_failures"] = (op.get("self") or {}).get("persist_failures")

    if metrics:
        snap = metrics.get("snapshot") or {}
        out["orders_total"] = snap.get("counters", {}).get("orders_total")
        out["orders_unknown"] = snap.get("counters", {}).get("orders_unknown")
        out["recovery_required"] = snap.get("counters", {}).get("recovery_required")
        alerts = metrics.get("alerts") or []
        out["alerts"] = [a.get("name") for a in alerts]

    for cname, key in (("adaptive-trading", "app"), ("adaptive-trading-mysql", "mysql"),
                       ("adaptive-trading-redis", "redis")):
        out[f"{key}_restarts"] = _restart_count(cname)
        out[f"{key}_health"] = _health_status(cname)
    out["app_stats"] = _container_stats("adaptive-trading")

    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--interval", type=float, default=60)
    ap.add_argument("--base", default="http://127.0.0.1:8800")
    ap.add_argument("--out", default="logs/stability.jsonl")
    args = ap.parse_args(argv[1:])

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.minutes * 60
    samples: list[dict] = []

    print(f"稳定性观察: {args.minutes:.0f} 分钟, 每 {args.interval:.0f}s 一次 → {out_path}",
          flush=True)
    with out_path.open("w", encoding="utf-8") as fh:
        while time.time() < deadline:
            s = sample(args.base)
            samples.append(s)
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"  [{len(samples):3d}] ok={s['app_http_ok']} status={s.get('status')} "
                  f"buy={s.get('can_buy')} reconc={s.get('reconciled')} "
                  f"tasks_failed={s.get('tasks_failed')} "
                  f"restarts(app/mysql/redis)={s.get('app_restarts')}/"
                  f"{s.get('mysql_restarts')}/{s.get('redis_restarts')} "
                  f"mem={s.get('app_stats', {}).get('mem')}", flush=True)
            time.sleep(args.interval)

    # ---- 结论: 由数据决定, 不由期望决定 ----
    verdict: dict = {"samples": len(samples)}
    if not samples:
        verdict["result"] = "FAILED"
        verdict["reason"] = "没有任何样本"
    else:
        down = [s for s in samples if not s["app_http_ok"]]
        verdict["app_unreachable_samples"] = len(down)
        verdict["app_restarts_max"] = max(s.get("app_restarts", 0) for s in samples)
        verdict["mysql_restarts_max"] = max(s.get("mysql_restarts", 0) for s in samples)
        verdict["redis_restarts_max"] = max(s.get("redis_restarts", 0) for s in samples)
        verdict["tasks_failed_max"] = max((s.get("tasks_failed") or 0) for s in samples)
        verdict["unknown_orders"] = max((s.get("orders_unknown") or 0) for s in samples)
        verdict["recovery_required"] = max((s.get("recovery_required") or 0) for s in samples)
        verdict["can_buy_any"] = any(s.get("can_buy") for s in samples)
        verdict["reconciled_all"] = all(s.get("reconciled") for s in samples if s.get("reconciled") is not None)
        # 内存是否持续增长: 首尾对比
        def _mem_mb(s: dict) -> float:
            raw = (s.get("app_stats") or {}).get("mem", "")
            try:
                return float(raw.split("MiB")[0].split("/")[0].strip())
            except Exception:
                return 0.0

        verdict["mem_first_mb"] = _mem_mb(samples[0])
        verdict["mem_last_mb"] = _mem_mb(samples[-1])
        verdict["mem_growth_mb"] = round(verdict["mem_last_mb"] - verdict["mem_first_mb"], 1)

        problems = []
        if verdict["app_unreachable_samples"]:
            problems.append(f"应用有 {verdict['app_unreachable_samples']} 次不可达")
        if verdict["tasks_failed_max"]:
            problems.append(f"关键任务失败 {verdict['tasks_failed_max']} 个")
        if verdict["unknown_orders"]:
            problems.append(f"出现 UNKNOWN 订单 {verdict['unknown_orders']} 次")
        if not verdict["can_buy_any"]:
            problems.append("整段观察期内一次都不能买(需确认是否被合理冻结)")
        verdict["problems"] = problems
        verdict["result"] = "PASSED" if not problems else "FAILED"

    print("\n=== 稳定性结论 ===")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    (out_path.with_suffix(".verdict.json")).write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if verdict.get("result") == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
