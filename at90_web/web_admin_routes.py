"""个人管理页面与配置管理 API(V12.3)。

页面定位(与 Dashboard 分工):
- `/`      —— 看状态(只读)。
- `/admin` —— 改配置、切模式、看开关解释、执行管理操作。
- `/ops`   —— 上线前只读检查。

**安全边界**
- `GET /api/admin/config` 只读, 无需令牌(只返回非敏感项, 敏感项显示 `已配置/未配置`)。
- `draft` / `apply` / `rollback` 是写操作, 一律需 `X-Admin-Token`。
- 校验与写入全部委托给 `at01_common.config_store`, 后者复用 `settings.validate()` /
  `mainnet_blocked_reason()` / `mainnet_readiness_check()` —— 与启动期判定同源,
  管理页面**没有**任何绕过主网守卫的通道。
- `apply` 只落盘, 不热改运行中的进程; 响应明确要求重启。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import HTMLResponse

from at01_common.config_store import (
    build_config_view,
    build_draft,
    changed_env_values,
    config_path_status,
    list_backups,
    proposed_env_values,
    resolve_config_path,
    rollback,
    write_env_values,
)
from at01_common.config_store import _to_ui
from at01_common.runtime_config import override_allowlist, rollback_overrides, save_overrides
from at01_common.settings import get_settings
from at90_web.web_auth import require_admin

admin_router = APIRouter()

_STATIC_DIR = Path(__file__).parent / "static"


def _restart_hint() -> dict[str, str]:
    """按运行载体给出重启方式(容器内 / 裸跑)。"""
    if Path("/.dockerenv").exists():
        return {
            "kind": "docker",
            "command": "docker compose --env-file /etc/adaptive-trading/production.env up -d",
            "text": "配置已保存到宿主机的配置文件, 需重建容器后生效。",
        }
    return {
        "kind": "process",
        "command": "重新启动 python run.py",
        "text": "配置已保存, 需重启进程后生效。",
    }


@admin_router.get("/admin", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    """个人管理页面(配置 / 模式切换 / 管理操作)。"""
    html = (_STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@admin_router.get("/api/admin/config")
async def admin_config() -> dict[str, Any]:
    """当前配置摘要 + 可编辑字段定义。**只读, 不需令牌。**

    敏感项(`*_KEY` / `*_SECRET` / `*_TOKEN`)只返回「已配置 / 未配置」, 绝不回显值。
    """
    settings = get_settings()
    path = resolve_config_path()
    view = build_config_view(settings, path)
    view["backups"] = [p.name for p in list_backups(path)]
    view["restart_hint"] = _restart_hint()
    return view


@admin_router.post("/api/admin/config/draft", dependencies=[Depends(require_admin)])
async def admin_config_draft(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """校验草稿: 返回 diff / 风险提示 / 是否需重启。**不写任何文件。**

    `payload.changes` 为 `{ENV_KEY: 新值}`。
    """
    changes = payload.get("changes") or {}
    if not isinstance(changes, dict):
        return {"ok": False, "problems": [{"key": "", "message": "changes 必须是对象"}],
                "diff": [], "risk_warnings": [], "requires_restart": False}
    draft = build_draft(base_settings=get_settings(), proposed=changes, path=resolve_config_path())
    draft["config"] = config_path_status()
    return draft


@admin_router.post("/api/admin/config/apply", dependencies=[Depends(require_admin)])
async def admin_config_apply(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """保存配置。**只落盘/落库, 不热改运行时。**

    V12.6 P1 起分两条路径:
    - **白名单键 → 数据库**(`runtime_config` 表, 启动时叠加; 优先级 DB > env > default)。
      好处: Pi 上不再需要挂载可写配置目录 —— DB 本就落在持久化卷上。
    - 其余键(密钥 / bootstrap 关键项 / 不可编辑) → 仍写 env 文件。

    两条路径都**先校验后写**, 校验不通过一个字节都不落。
    """
    changes = payload.get("changes") or {}
    if not isinstance(changes, dict):
        return {"ok": False, "message": "changes 必须是对象"}

    path = resolve_config_path()
    status = config_path_status(path)
    by = str(payload.get("by") or "admin")[:64]
    reason = str(payload.get("reason") or "")[:512]

    # 校验对**全部** changes 生效(与走哪条存储路径无关): 不通过就不写任何一处
    draft = build_draft(base_settings=get_settings(), proposed=changes, path=path)
    if not draft.get("ok"):
        return {
            "ok": False,
            "stage": "validate",
            "message": "配置校验未通过, 未写入任何文件。",
            "problems": draft.get("problems") or [],
            "blocked_reasons": draft.get("blocked_reasons") or [],
        }

    allowed = override_allowlist()
    db_changes = {k: v for k, v in changes.items() if k in allowed}
    file_changes = {k: v for k, v in changes.items() if k not in allowed}

    written_db: dict[str, Any] = {}
    if db_changes:
        saved = await save_overrides(db_changes, by=by, reason=reason)
        if not saved.get("ok"):
            return {"ok": False, "stage": "db", "message": saved.get("error", "写入数据库失败。")}
        written_db = saved["written"]

    # 文件路径: 仅当还有 env-only 键时才需要可写
    written_file: dict[str, Any] = {}
    backup: str | None = None
    if file_changes:
        if not status["writable"]:
            return {
                "ok": False,
                "stage": "write",
                "message": (
                    f"配置文件 {status['path']} 当前不可写(容器内未挂载该路径, 或权限不足)。"
                    "请在宿主机修改该文件后重启服务。"
                    f"(本次有 {len(written_db)} 项已写入数据库, 但含密钥类字段时必须写文件)"
                ),
                "config": status,
            }
        env_values = proposed_env_values(file_changes)
        to_write = changed_env_values(path, env_values)
        if to_write:
            result = write_env_values(path, to_write)
            written_file = result["written"]
            backup = result["backup"]

    if not written_db and not written_file:
        return {"ok": True, "changed": False, "message": "配置与当前值一致, 无需写入。",
                "requires_restart": False, "restart_hint": _restart_hint()}

    return {
        "ok": True,
        "changed": True,
        "storage": {"database": sorted(written_db), "file": sorted(written_file)},
        "written": written_file,
        "backup": backup,
        "diff": draft.get("diff") or [],
        "risk_warnings": draft.get("risk_warnings") or [],
        "requires_restart": True,
        "restart_hint": _restart_hint(),
        "message": (
            f"已保存({len(written_db)} 项入数据库 / {len(written_file)} 项入配置文件), "
            "需重启服务/容器生效。"
        ),
        "config": config_path_status(path),
    }


@admin_router.post("/api/admin/config/rollback", dependencies=[Depends(require_admin)])
async def admin_config_rollback() -> dict[str, Any]:
    """恢复上一份配置: **同时回退文件与数据库两层**。

    V12.6 P1: 配置分两层存储后, 只恢复文件会让 DB 覆盖活下来 —— 操作者点了「恢复上一份
    配置」却发现值没回去。那是典型的「以为回滚了其实没有」, 必须避免。
    因此本接口恢复文件备份的**同时按审计逐步回退数据库覆盖**。
    """
    path = resolve_config_path()
    status = config_path_status(path)

    # 两层各自回退, 成败**合并判定** —— 只回退成功一层也算部分成功, 如实报告。
    # (常见情形: P1 之后可编辑字段全走 DB, 文件侧根本没有备份可供恢复。)
    reverted = await rollback_overrides()
    file_result: dict[str, Any] = {"ok": False}
    if status["writable"]:
        file_result = rollback(path)

    db_ok = bool(reverted["count"])
    file_ok = bool(file_result.get("ok"))
    if not db_ok and not file_ok:
        return {
            "ok": False,
            "stage": "write" if not status["writable"] else "rollback",
            "message": (
                "没有可回退的内容"
                + ("(配置文件不可写且数据库无覆盖)" if not status["writable"]
                   else "(文件无备份且数据库无覆盖)")
            ),
            "config": status,
        }

    parts = []
    if file_ok:
        parts.append("文件备份已恢复")
    if db_ok:
        parts.append(f"数据库覆盖已回退 {reverted['count']} 项")
    return {
        "ok": True,
        "file_rolled_back": file_ok,
        "database_reverted": reverted["reverted"],
        "backup": file_result.get("backup"),
        "requires_restart": True,
        "restart_hint": _restart_hint(),
        "message": "、".join(parts) + ", 需重启服务/容器生效。",
        "config": config_path_status(path),
    }


@admin_router.get("/api/admin/auth-check", dependencies=[Depends(require_admin)])
async def admin_auth_check() -> dict[str, Any]:
    """令牌校验探针: 通过鉴权依赖即返回 200。

    页面用它把「令牌未填写 / 无效 / 有效」明确显示出来, 而不是等用户点了按钮才吃 401。
    """
    return {"ok": True}


async def _preflight_restart() -> dict[str, Any]:
    """重启前自检: **重启后会生效的整份配置**必须能通过同一套启动守卫。

    防的是最常见也最难受的一种翻车: 页面上存了一份过不了守卫的配置, 一重启进程就起不来,
    连页面都没了, 只能 SSH 上去手工恢复。

    V12.6 P1: 配置分两层存储后, **必须把数据库覆盖一并纳入**。
    只读文件会让"存了一份起不来的 DB 覆盖"绕过这道防线 —— 而那恰恰是重启后
    连页面都打不开的情形。
    """
    from at01_common.runtime_config import load_overrides, override_allowlist

    path = resolve_config_path()
    persisted = build_config_view(get_settings(), path)
    proposed: dict[str, Any] = {}
    for f in persisted["fields"]:
        if f.get("sensitive") or not f.get("editable"):
            continue
        if f.get("in_file"):
            proposed[f["key"]] = f["value"]

    # 数据库覆盖(优先级高于文件) —— 用 UI 域喂给 build_draft, 与页面提交同域
    allowed = override_allowlist()
    for key, raw in (await load_overrides()).items():
        spec = allowed.get(key)
        if spec is not None:
            proposed[key] = _to_ui(spec, raw)

    return build_draft(base_settings=get_settings(), proposed=proposed, path=path)


@admin_router.get("/api/admin/reload-status")
async def admin_reload_status() -> dict[str, Any]:
    """V13 P1: 「配置已更新 → 系统自动重启 → 自动验证 → 恢复无人值守」的**进度与结论**。

    任务书要求页面只告诉用户:

        配置已更新 / 系统正在重新加载……
        ✓ 配置验证 ✓ 服务重启 ✓ 数据库正常 ✓ 风控正常 ✓ 对账正常
        系统已恢复无人值守。

    实现方式是**读事件流**而不是让页面自己轮询各种接口拼结论: 启动过程中的
    「系统启动 / 行情连接 / 就绪」本来就是操作员事件流在记的东西, 复用它们
    才不会出现「页面显示的和日志说的不一样」。

    只读接口, 无任何写路径。
    """
    import time as _time

    from at01_common.operator_events import (
        KIND_CONNECT, KIND_READY, KIND_STARTUP, operator_log,
    )
    from at01_common.runtime_health import build_runtime_health
    from at01_common.settings import get_settings
    from at90_web.web_state import system_state

    events = operator_log.recent(200)
    if not events:
        events = await operator_log.load_recent(200)

    # 事件流是「最新在前」; 找出最近一次启动及其之后的全部事件
    startup_ts = 0
    for e in events:
        if e.get("kind") == KIND_STARTUP:
            startup_ts = int(e.get("ts") or 0)
            break
    since = [e for e in events if int(e.get("ts") or 0) >= startup_ts] if startup_ts else []
    kinds = {e.get("kind") for e in since}

    try:
        health = build_runtime_health(system_state)
    except Exception:
        health = {}
    reconcile = health.get("reconcile") or {}
    settings = get_settings()
    try:
        problems = list(settings.validate())
    except Exception:
        problems = ["配置校验未能完成"]

    steps = [
        {"key": "config", "label": "配置验证", "ok": not problems,
         "detail": "; ".join(problems[:3]) if problems else "配置合法"},
        {"key": "restart", "label": "服务重启", "ok": bool(startup_ts),
         "detail": "进程已重新启动" if startup_ts else "尚未观察到启动事件"},
        {"key": "database", "label": "数据库", "ok": bool(system_state.running),
         "detail": "已连接" if system_state.running else "未连接"},
        {"key": "exchange", "label": "行情连接", "ok": KIND_CONNECT in kinds or not since,
         "detail": "行情数据源已连接" if KIND_CONNECT in kinds else "等待连接"},
        {"key": "risk", "label": "风控", "ok": bool(health.get("can_sell", False)) or not health,
         "detail": "风控已就绪"},
        {"key": "reconcile", "label": "对账", "ok": bool(reconcile.get("reconciled")),
         "detail": "对账通过" if reconcile.get("reconciled") else "对账进行中"},
    ]
    ready = KIND_READY in kinds
    pending = [s["label"] for s in steps if not s["ok"]]
    return {
        "ok": True,
        "ready": ready and not pending,
        "steps": steps,
        "pending": pending,
        "started_at": startup_ts,
        "uptime_seconds": health.get("uptime_seconds"),
        "conclusion": (
            "系统已恢复无人值守。" if (ready and not pending)
            else ("配置有问题, 需要修正。" if problems
                  else "正在重新加载……" + (f"(等待: {'、'.join(pending)})" if pending else ""))
        ),
        "as_of": _time.time(),
    }


@admin_router.post("/api/admin/restart", dependencies=[Depends(require_admin)])
async def admin_restart() -> dict[str, Any]:
    """重启服务, 让已保存的配置生效。

    实现方式: 复用与 Ctrl+C / `docker stop` **完全相同**的优雅停机路径
    (`runtime.request_shutdown()` → `system.stop()`), 由容器的 `restart: unless-stopped`
    把进程拉起来。

    **诚实边界**: 本进程无法保证自己一定会回来。
    - 容器内 → 重启策略会拉起(响应里如实说明);
    - 非容器 → 进程退出后**不会**自动回来, 响应会明确提示需要手动启动。
    """
    from at01_common.runtime import in_container, request_shutdown

    draft = await _preflight_restart()
    if not draft.get("ok"):
        return {
            "ok": False,
            "stage": "preflight",
            "message": "当前配置未通过启动守卫, 已拒绝重启 —— 否则服务可能起不来。"
                       "请先修正配置或回滚。",
            "problems": draft.get("problems") or [],
            "blocked_reasons": draft.get("blocked_reasons") or [],
        }

    containerized = in_container()
    delivered = request_shutdown()
    if not delivered:
        return {
            "ok": False,
            "stage": "signal",
            "message": "无法投递停机请求(当前进程未运行在主循环中); 请在宿主机重启服务。",
        }
    return {
        "ok": True,
        "containerized": containerized,
        "message": (
            "已请求优雅停机, 容器将自动重启并加载新配置(约 10~30 秒)。"
            if containerized else
            "已请求优雅停机。**当前不是容器运行**, 进程退出后不会自动拉起, "
            "请手动重新启动(如 python run.py)。"
        ),
        "restart_hint": _restart_hint(),
    }
