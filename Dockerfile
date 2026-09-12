# ======================================================================
# V11.8 §3: 生产 Dockerfile
#
# - 基础镜像 python:3.13-slim(多架构 amd64/arm64, 由 buildx --platform 指定)
# - 依赖用 uv sync --frozen 从 uv.lock 可复现安装(不含 dev), 不装项目本身
#   (扁平 atXX 目录、无 build-system, 由 run.py bootstrap 注入 sys.path)
# - 多阶段: builder 装依赖, 运行层精简 + 非 root(app 用户)
# - tini 作 PID 1(信号转发 + 僵尸回收), docker stop → SIGTERM → run.py 优雅停机
# - 无 .env / 密钥进镜像(env 由 compose env_file 注入; .dockerignore 兜底排除)
#
# 可覆盖构建参数(镜像源/依赖源, 供防火墙/镜像站环境):
#   docker build --build-arg PYTHON_BASE=镜像站/python:3.13-slim \
#                --build-arg PIP_INDEX_URL=镜像站/simple \
#                --build-arg UV_DEFAULT_INDEX=镜像站/simple -t adaptive-trading:<git_sha> .
# ======================================================================

ARG PYTHON_BASE=python:3.13-slim
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

FROM ${PYTHON_BASE} AS builder

ARG PIP_INDEX_URL
ARG UV_DEFAULT_INDEX

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 固定 uv 版本(与 uv.lock 生成版本一致, 保证可复现构建)
ARG UV_VERSION=0.11.15
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" "uv==${UV_VERSION}"

WORKDIR /app

# 先拷依赖清单 + lockfile(利用层缓存: 依赖不变则无需重装)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ----------------------------------------------------------------------
FROM ${PYTHON_BASE}

# 版本追踪(V11.8 §7): 容器内无 .git, 运行时 `git rev-parse HEAD` 会空; 构建时把 git SHA
# 打进镜像(ARG/ENV), 使启动日志/主网就绪自检/soak 证据能还原「镜像 → git SHA」。
#   docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t adaptive-trading:<sha> .
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    TZ=UTC

# tini: 正确的 PID 1(SIGTERM 转发 + 僵尸回收), 保证优雅停机
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行(app 用户)
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --create-home app

WORKDIR /app

# 虚拟环境(与 builder 同基础镜像, 相对路径/符号链接可跨层复用)+ 源码 + 迁移脚本
COPY --from=builder --chown=app:app /app/.venv /app/.venv
# 源码分层复制(按阅读顺序 = 数据流顺序, 见 docs/architecture.md)。
# 注: Dockerfile 的 COPY **不支持行尾注释** —— `#` 之后会被当成额外的源路径,
#     构建时报 `"/L9": not found` 这类错误(V12.6 曾误加行尾注释, V12.9 实测构建才发现)。
COPY --chown=app:app run.py ./
# 按层号排序(阅读顺序 = 数据流顺序, 见 docs/architecture.md)
COPY --chown=app:app at01_common/    ./at01_common/
COPY --chown=app:app at10_market/    ./at10_market/
COPY --chown=app:app at20_analytics/ ./at20_analytics/
COPY --chown=app:app at30_strategy/  ./at30_strategy/
COPY --chown=app:app at40_portfolio/ ./at40_portfolio/
COPY --chown=app:app at50_risk/      ./at50_risk/
COPY --chown=app:app at60_execution/ ./at60_execution/
COPY --chown=app:app at70_journal/   ./at70_journal/
COPY --chown=app:app at80_backtest/  ./at80_backtest/
COPY --chown=app:app at85_optimizer/ ./at85_optimizer/
COPY --chown=app:app at90_web/       ./at90_web/
COPY --chown=app:app migrations/ ./migrations/

# 数据 / 日志 / 证据目录(compose 挂载卷覆盖; 默认空)
RUN mkdir -p /app/data /app/logs /app/evidence \
    && chown -R app:app /app

USER app

EXPOSE 8800

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
