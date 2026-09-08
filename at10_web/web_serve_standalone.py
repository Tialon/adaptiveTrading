"""
前端独立启动(15)

用法:
    python 10_web/web/serve/standalone.py          # 默认 8800
    python 10_web/web/serve/standalone.py --port 9000

场景:
- 只想看面板/查历史数据,不启动交易引擎
- 后端 run.py 已在别的机器上运行时,本机起面板连数据库只读

注意: 独立模式下实时行情/WS 推送为空态(引擎未注册),
数据库端点(orders/signals/绩效/收益曲线)正常可用。
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for d in ("at01_common", "at10_web", "at20_market", "at30_analytics", "at50_strategy", "at50_execution", "at60_risk"):
    sys.path.insert(0, str(ROOT / d))


async def main() -> None:
    parser = argparse.ArgumentParser(description="adaptiveTrading 前端面板(独立模式)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    from at01_common.settings import get_settings
    from at01_common.logger import setup_logging

    setup_logging()

    settings = get_settings()
    port = args.port or settings.api_port

    # 建表(若连的库还没有表)
    from at01_common.database import init_db

    await init_db()

    from at10_web.web_app import start_server

    print(f"面板(独立模式): http://localhost:{port}")
    print("实时行情为空态(交易引擎未运行), 历史数据端点可用")
    await start_server(args.host, port)


if __name__ == "__main__":
    asyncio.run(main())
