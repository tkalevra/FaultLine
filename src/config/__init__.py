"""FaultLine Configuration Package.

This package centralizes all configuration management.

Usage:
    from src.config.settings import settings

    # Access any setting
    poll_interval = settings.PROVISIONING_POLL_INTERVAL
    dsn = settings.POSTGRES_DSN
    batch_size = settings.PROVISIONING_BATCH_SIZE
"""

from .settings import settings

__all__ = ["settings"]
