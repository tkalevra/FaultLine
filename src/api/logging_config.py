"""
Logging classification system: CRIT | WARN | INFO | DEBUG
Failures are exposed by default (non-DEBUG). DEBUG level controlled via OpenWebUI valve.
"""

import os
import structlog
from enum import Enum


class LogLevel(str, Enum):
    """Logging levels with hierarchy."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    CRIT = "CRIT"


# Global logging level (controllable via OpenWebUI valve)
_CURRENT_LOG_LEVEL = LogLevel(os.getenv("FAULTLINE_LOG_LEVEL", "INFO"))


def set_log_level(level: str) -> None:
    """Set logging level from OpenWebUI valve. Called at runtime."""
    global _CURRENT_LOG_LEVEL
    try:
        _CURRENT_LOG_LEVEL = LogLevel(level.upper())
        structlog.get_logger().info(
            "logging.level_changed",
            new_level=_CURRENT_LOG_LEVEL,
            source="openwebui_valve"
        )
    except ValueError:
        structlog.get_logger().warn(
            "logging.invalid_level",
            attempted_level=level,
            valid_levels=list(LogLevel)
        )


def get_log_level() -> LogLevel:
    """Get current logging level."""
    return _CURRENT_LOG_LEVEL


def should_log(level: LogLevel) -> bool:
    """Check if a message at this level should be logged."""
    level_order = {LogLevel.DEBUG: 0, LogLevel.INFO: 1, LogLevel.WARN: 2, LogLevel.CRIT: 3}
    return level_order[level] >= level_order[_CURRENT_LOG_LEVEL]


def log_crit(logger, msg: str, **args):
    """Always log CRIT — system failures."""
    logger.critical(msg, log_level="CRIT", **args)


def log_warn(logger, msg: str, **args):
    """Always log WARN — degraded but working."""
    logger.warning(msg, log_level="WARN", **args)


def log_info(logger, msg: str, **args):
    """Log INFO — informational messages."""
    if should_log(LogLevel.INFO):
        logger.info(msg, log_level="INFO", **args)


def log_debug(logger, msg: str, **args):
    """Log DEBUG only if DEBUG level enabled via valve."""
    if should_log(LogLevel.DEBUG):
        logger.debug(msg, log_level="DEBUG", **args)


def configure_structlog():
    """Configure structlog with classification."""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
