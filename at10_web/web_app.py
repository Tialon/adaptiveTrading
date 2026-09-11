"""
Web 应用装配层(10_web 顶层)

组装:
- 11 state  (运行时状态)
- 12 api    (REST 路由)
- 13 ws     (WebSocket 推送)
- 14 static (面板静态文件,由 api 层 / 提供)

提供:
- app          FastAPI 实例(被 run.py 挂载)
- start_server 启动服务(随主程序)
"""

from fastapi import FastAPI

from at01_common.settings import get_settings
from at10_web.web_admin_routes import admin_router
from at10_web.web_api_routes import router as api_router
from at10_web.web_ws_stream import ws_router


def create_app() -> FastAPI:
    """构建 FastAPI 应用"""
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)
    app.include_router(api_router)
    app.include_router(admin_router)
    app.include_router(ws_router)
    return app


app = create_app()


async def start_server(host: str = "127.0.0.1", port: int = 8800) -> None:
    """启动 API 服务(主程序内嵌启动)"""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
