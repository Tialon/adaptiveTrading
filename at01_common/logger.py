"""
日志配置(structlog)
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from at01_common.settings import get_settings


def setup_logging() -> None:
    """初始化日志"""
    settings = get_settings()

    # 文件 handler
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        log_path = Path(settings.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 轮转日志: 单文件 10MB, 保留 5 份, 防无人值守下磁盘写满
        handlers.append(
            RotatingFileHandler(
                log_path, encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=5
            )
        )

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, handlers=handlers, force=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer() if settings.debug else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


class LoggerMixin:
    """为类提供 self.logger"""

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        if not hasattr(self, "_logger"):
            self._logger = structlog.get_logger(self.__class__.__name__)
        return self._logger


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取命名 logger"""
    return structlog.get_logger(name)
