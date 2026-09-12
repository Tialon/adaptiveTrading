"""运行参数入库(V12.6 P1)

**目标**: `/admin` 编辑的配置存数据库, 使 Pi 上改配置**不再需要**挂载可写配置目录
(此前需 `chown -R 999:999 /etc/adaptive-trading`)。DB 本来就落在持久化卷上。

**优先级: DB > env > default**

三条硬边界(每一条都有对应测试):

1. **密钥永不入库** —— 复用 `config_store.is_sensitive_key`(含 KEY/SECRET/TOKEN/PASSWORD)。
   DB 会被备份、被拷来拷去, 密钥一旦入库就等于扩散。
2. **bootstrap 关键项排除** —— `DATABASE_URL` 等必须在数据库连上**之前**就有效,
   存在自己指向的库里是循环依赖。
3. **不热生效** —— 本模块只在**启动时**把 DB 值叠加到 Settings 上。
   多数参数是构造期读取的(策略/指标窗口在 `__init__` 里取), 真正热生效需要重构那些组件。
   页面按 `FieldSpec.restart_required` 如实提示, **不谎称已生效**。

**审计**: 每次写入同时落 `runtime_config_history`。改风控阈值就是改交易行为,
必须能回答「谁在什么时候把哪个值改成了什么、为什么」。
"""

from __future__ import annotations

from typing import Any

from at01_common.config_store import (
    _settings_to_ui,
    FIELD_SPECS,
    FieldSpec,
    _to_env,
    _to_ui,
    _ui_to_settings,
    is_sensitive_key,
)

# 必须在数据库连上之前就生效的键 —— 存进它们自己指向的库是循环依赖。
BOOTSTRAP_CRITICAL: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "API_HOST",
        "API_PORT",
        "WEB_ADMIN_TOKEN",
        "ADAPTIVE_TRADING_ENV_FILE",
    }
)


def override_allowlist() -> dict[str, FieldSpec]:
    """可入库的字段: 可编辑 + 非密钥 + 非 bootstrap 关键。`key -> FieldSpec`。"""
    return {
        spec.key: spec
        for spec in FIELD_SPECS
        if spec.editable
        and not is_sensitive_key(spec.key)
        and spec.key not in BOOTSTRAP_CRITICAL
    }


def env_value_to_settings(spec: FieldSpec, raw: str) -> Any:
    """DB 里存的是 **env 域** 字符串(与 `write_env_values` 一致), 转成 Settings 域的值。"""
    return _ui_to_settings(spec, _to_ui(spec, raw))


def settings_value_to_env(spec: FieldSpec, settings: Any) -> str:
    """Settings 当前值 -> env 域字符串(用于与 DB 值比较、以及写库)。

    ⚠️ 必须**先经 `_settings_to_ui`** 回到 UI 域再 `_to_env` —— `_to_env` 假定入参是
    UI 域(百分比是 `3` 而非 `0.03`), 直接喂 Settings 域会被再除一次 100,
    把 0.03 变成 0.0003。这个坑在 V12.3 的管理页面里已经踩过一次, 有测试锚定。
    """
    return _to_env(spec, _settings_to_ui(spec, getattr(settings, spec.attr, None)))


async def load_overrides() -> dict[str, str]:
    """读取 DB 里的**全部**覆盖行(不过滤)。

    过滤交给 `apply_overrides` —— 这样「白名单收缩后遗留的孤儿行」是可观测的
    (会被报进 `skipped`), 而不是被静默吞掉。DB 不可用 -> 返回空(优雅降级到 env)。
    """
    from sqlalchemy import select

    from at01_common.database import AsyncSessionLocal
    from at01_common.models import RuntimeConfig

    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(RuntimeConfig))).scalars().all()
    except Exception:
        return {}
    return {r.key: r.value for r in rows}


async def apply_overrides(settings: Any) -> dict[str, Any]:
    """启动时把 DB 覆盖值叠加到 Settings 单例上。返回报告。

    **调用方必须在之后重跑 `settings.validate()`** —— 否则一个非法的 DB 值会绕过
    启动期 fail-fast; 必须在应用后再校验一次合并结果。
    """
    overrides = await load_overrides()
    applied: dict[str, Any] = {}
    skipped: dict[str, str] = {}

    for key, raw in overrides.items():
        spec = override_allowlist().get(key)
        if spec is None:  # 白名单收缩后的历史残留
            skipped[key] = "已不在允许入库的白名单内"
            continue
        try:
            setattr(settings, spec.attr, env_value_to_settings(spec, raw))
            applied[spec.attr] = raw
        except Exception as e:  # 单个键坏掉不应阻断启动(会在 re-validate 时暴露)
            skipped[key] = f"应用失败: {e}"

    return {"applied": applied, "skipped": skipped, "count": len(applied)}


async def save_overrides(
    changes: dict[str, Any], *, by: str = "", reason: str = ""
) -> dict[str, Any]:
    """写入覆盖值 + 落审计。

    `changes` 收 **UI 域**的值(`{ENV键: UI值}`, 百分比是 `3` 而非 `0.03`)—— 与调用方
    (管理页面)的自然域一致, 转换在内部用 `_to_env` 完成。**库内存 env 域**
    (与 `write_env_values` 一致, 也与 `Settings` 直接解析的形态一致)。

    非白名单键一律拒绝(不静默忽略) —— 否则页面提交一个密钥字段就会被写进库。
    """
    from sqlalchemy import select

    from at01_common.database import AsyncSessionLocal
    from at01_common.models import RuntimeConfig, RuntimeConfigHistory

    allowed = override_allowlist()
    rejected = [k for k in changes if k not in allowed]
    if rejected:
        return {
            "ok": False,
            "error": "以下键不允许写入数据库(密钥 / bootstrap 关键项 / 不可编辑): "
            + ", ".join(sorted(rejected)),
            "rejected": rejected,
        }

    # UI 域 -> env 域(库内存储形态)
    env_changes = {k: _to_env(allowed[k], v) for k, v in changes.items()}

    written: dict[str, str] = {}
    async with AsyncSessionLocal() as session:
        for key, new_value in env_changes.items():
            row = (
                await session.execute(select(RuntimeConfig).where(RuntimeConfig.key == key))
            ).scalars().first()
            old_value = row.value if row else ""
            if row is None:
                session.add(
                    RuntimeConfig(key=key, value=new_value, updated_by=by, reason=reason)
                )
            else:
                row.value = new_value
                row.updated_by = by
                row.reason = reason
            if old_value != new_value:
                session.add(
                    RuntimeConfigHistory(
                        key=key,
                        old_value=old_value,
                        new_value=new_value,
                        changed_by=by,
                        reason=reason,
                    )
                )
            written[key] = new_value
        await session.commit()

    return {"ok": True, "written": written, "count": len(written)}


async def rollback_overrides() -> dict[str, Any]:
    """把每个键**撤销到它最近一次变更之前的值**(一步撤销)。

    语义选择: 不是「清空所有覆盖」—— 那会把与本次回退无关的、早先特意设过的覆盖
    一起丢掉, 属意外破坏。这里按 `runtime_config_history` 里每个键的最近一条记录
    回退; 原值为空(即该键此前没有覆盖)则删除该行, 回到 env/默认。

    **不是删除数据**: 回退本身也会记入审计。
    """
    from sqlalchemy import select

    from at01_common.database import AsyncSessionLocal
    from at01_common.models import RuntimeConfig, RuntimeConfigHistory

    reverted: dict[str, Any] = {}
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(RuntimeConfigHistory).order_by(RuntimeConfigHistory.id.desc())
            )
        ).scalars().all()

        seen: set[str] = set()
        for h in rows:
            if h.key in seen:  # 只看每个键最近的一条
                continue
            seen.add(h.key)
            row = (
                await session.execute(select(RuntimeConfig).where(RuntimeConfig.key == h.key))
            ).scalars().first()
            if row is None or row.value != h.new_value:
                continue  # 该键此后又被改过或已不存在 —— 不动它
            if h.old_value == "":
                await session.delete(row)
                reverted[h.key] = None
            else:
                row.value = h.old_value
                reverted[h.key] = h.old_value
            session.add(
                RuntimeConfigHistory(
                    key=h.key,
                    old_value=h.new_value,
                    new_value=h.old_value,
                    changed_by="rollback",
                    reason="回退上一份配置",
                )
            )
        await session.commit()
    return {"reverted": reverted, "count": len(reverted)}


async def config_history(limit: int = 50) -> list[dict[str, Any]]:
    """最近变更(倒序)。"""
    from sqlalchemy import select

    from at01_common.database import AsyncSessionLocal
    from at01_common.models import RuntimeConfigHistory

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(RuntimeConfigHistory)
                    .order_by(RuntimeConfigHistory.id.desc())
                    .limit(min(limit, 200))
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "key": r.key,
            "old_value": r.old_value,
            "new_value": r.new_value,
            "changed_at": str(r.changed_at),
            "changed_by": r.changed_by,
            "reason": r.reason,
        }
        for r in rows
    ]
