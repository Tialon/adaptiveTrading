# adaptiveTrading 应用镜像
# 分层目录(号码前缀)原样打进镜像,run.py 注入 sys.path

FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 依赖(单独一层,利用缓存)
COPY pyproject.toml ./
RUN pip install aiohttp "sqlalchemy[asyncio]" aiomysql redis cryptography \
    pydantic pydantic-settings python-dotenv \
    fastapi "uvicorn[standard]" structlog

# 分层模块(与目录号码对应)
COPY at01_common/ ./at01_common/
COPY at10_web/ ./at10_web/
COPY at20_market/ ./at20_market/
COPY at30_analytics/ ./at30_analytics/
COPY at50_strategy/ ./at50_strategy/
COPY at50_execution/ ./at50_execution/
COPY at60_risk/ ./at60_risk/
COPY at70_backtest/ ./at70_backtest/
COPY run.py ./

RUN mkdir -p logs

EXPOSE 8800

CMD ["python", "run.py"]
