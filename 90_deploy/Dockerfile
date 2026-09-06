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
COPY 00_common/ ./00_common/
COPY 10_web/ ./10_web/
COPY 20_market/ ./20_market/
COPY 30_ayalytics/ ./30_ayalytics/
COPY 50_startegy/ ./50_startegy/
COPY 50_execution/ ./50_execution/
COPY 60_risk/ ./60_risk/
COPY run.py ./

RUN mkdir -p logs

EXPOSE 8800

CMD ["python", "run.py"]
