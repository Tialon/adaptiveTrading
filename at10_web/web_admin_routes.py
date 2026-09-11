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
from at01_common.settings import get_settings
from at10_web.web_auth import require_admin

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
    """写入配置文件(先备份, 再原子替换)。**只落盘, 不热改运行时。**

    校验不通过 → 400(逐项 problems); 文件不可写 → 409 并给出可操作提示。
    """
    changes = payload.get("changes") or {}
    if not isinstance(changes, dict):
        return {"ok": False, "message": "changes 必须是对象"}

    path = resolve_config_path()
    status = config_path_status(path)

    draft = build_draft(base_settings=get_settings(), proposed=changes, path=path)
    if not draft.get("ok"):
        return {
            "ok": False,
            "stage": "validate",
            "message": "配置校验未通过, 未写入任何文件。",
            "problems": draft.get("problems") or [],
            "blocked_reasons": draft.get("blocked_reasons") or [],
        }

    if not status["writable"]:
        return {
            "ok": False,
            "stage": "write",
            "message": (
                f"配置文件 {status['path']} 当前不可写(容器内未挂载该路径, 或权限不足)。"
                "请在宿主机修改该文件后重启服务。"
            ),
            "config": status,
        }

    env_values = proposed_env_values(changes)
    to_write = changed_env_values(path, env_values)
    if not to_write:
        return {"ok": True, "changed": False, "message": "配置与当前文件一致, 无需写入。",
                "requires_restart": False, "restart_hint": _restart_hint()}

    result = write_env_values(path, to_write)
    return {
        "ok": True,
        "changed": True,
        "written": result["written"],
        "appended": result["appended"],
        "backup": result["backup"],
        "diff": draft.get("diff") or [],
        "risk_warnings": draft.get("risk_warnings") or [],
        "requires_restart": True,
        "restart_hint": _restart_hint(),
        "message": "配置已保存, 需重启服务/容器生效。",
        "config": config_path_status(path),
    }


@admin_router.post("/api/admin/config/rollback", dependencies=[Depends(require_admin)])
async def admin_config_rollback() -> dict[str, Any]:
    """恢复最近一份配置备份(恢复前会再备份当前文件, 可再次回退)。"""
    path = resolve_config_path()
    status = config_path_status(path)
    if not status["writable"]:
        return {"ok": False, "message": f"配置文件 {status['path']} 当前不可写, 无法回滚。",
                "config": status}
    result = rollback(path)
    if result.get("ok"):
        result["requires_restart"] = True
        result["restart_hint"] = _restart_hint()
        result["message"] = "已恢复上一份配置, 需重启服务/容器生效。"
    return result
