"""
Web 写接口鉴权(V11.5 P0-1)

单机系统, 不引入 OAuth/JWT/网关: 共享令牌 `WEB_ADMIN_TOKEN` 保护**所有写接口**
(POST/PUT/DELETE/PATCH)。GET 查询接口保持简单、无需鉴权。

安全语义(fail-closed):
- **显式关闭**(`WEB_ADMIN_AUTH=off`)→ 放行, 不校验令牌(个人局域网场景, 见下)。
- 未配置 `web_admin_token`(默认空)→ 写接口整体锁定(503), 提示设置 WEB_ADMIN_TOKEN。
- 配置了令牌但请求头 `X-Admin-Token` 缺失/不匹配 → 401。
- 令牌匹配 → 放行。

用 `secrets.compare_digest` 做常量时间比较, 防时序侧信道。

**V12.4 关于 `WEB_ADMIN_AUTH=off`**: 面向个人局域网单用户场景的显式逃生口。打开后
局域网内任何设备无需凭据即可调用写接口(含改配置、恢复急停、停机)。因此:
- 默认值仍是 `on`, 关闭必须**显式**写配置;
- 启动日志、`/admin`、`/` 面板常驻告警横幅, `/ops` 记为 WARN —— 避免「关了忘了」。
"""

import secrets

from fastapi import Header, HTTPException, status

from at01_common.settings import get_settings

_HEADER = "X-Admin-Token"


async def require_admin(x_admin_token: str = Header(default="", alias=_HEADER)) -> None:
    """写接口统一鉴权依赖: 校验 `X-Admin-Token` 头(显式关闭鉴权时直接放行)。"""
    settings = get_settings()
    if settings.admin_auth_disabled:
        return
    expected = settings.web_admin_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="管理员写操作未启用: 请在 .env 设置 WEB_ADMIN_TOKEN, 或设 WEB_ADMIN_AUTH=off 关闭鉴权",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权: X-Admin-Token 缺失或无效",
        )
