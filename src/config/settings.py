"""FaultLine Configuration - All settings via environment variables.

This module provides a centralized configuration interface for FaultLine.
All settings use plain-English environment variable names with sensible defaults.

Configuration sources (in order of precedence):
1. Environment variables (e.g., set in .env, docker-compose.yml, or CI/CD)
2. Values in this file (sensible defaults)

Non-technical users can understand all settings by reading the tooltip comments.
See .env.example for all available options and descriptions.

Example usage:
    from src.config.settings import settings

    poll_interval = settings.PROVISIONING_POLL_INTERVAL
    batch_size = settings.PROVISIONING_BATCH_SIZE
    dsn = settings.POSTGRES_DSN
"""

import os
from typing import Optional


class Settings:
    """FaultLine Configuration Settings - Read from environment variables."""

    # ================================================================
    # DATABASE CONFIGURATION
    # ================================================================

    @property
    def POSTGRES_DSN(self) -> str:
        """PostgreSQL connection string. Format: postgresql://user:pass@host:5432/database.

        This is the database that stores facts, entities, and relationships.
        Required for all operations. No default - must be set.
        """
        value = os.environ.get("POSTGRES_DSN", "")
        if not value:
            raise ValueError(
                "POSTGRES_DSN not set. "
                "Set via environment variable or .env file. "
                "Example: postgresql://user:pass@localhost:5432/faultline"
            )
        return value

    # ================================================================
    # PROVISIONING CONFIGURATION (User Schema Setup)
    # ================================================================

    @property
    def PROVISIONING_POLL_INTERVAL(self) -> int:
        """How often (in seconds) to check for new users to set up.

        Lower values = faster user setup, but more frequent database checks.
        Higher values = less frequent checking, but slower user onboarding.
        Default: 5 seconds (most systems should use this).
        """
        return int(os.environ.get("PROVISIONING_POLL_INTERVAL", "5"))

    @property
    def PROVISIONING_BATCH_SIZE(self) -> int:
        """How many user schemas to create in each batch.

        Higher values = faster overall provisioning, but uses more server resources.
        Lower values = slower provisioning, but uses less resources at once.
        Default: 10 users per batch.
        """
        return int(os.environ.get("PROVISIONING_BATCH_SIZE", "10"))

    @property
    def SCHEMA_NAME_PREFIX(self) -> str:
        """Prefix for user schema names in PostgreSQL.

        Each user gets a schema named: {PREFIX}_{user_id}
        Example: If PREFIX=faultline, user gets "faultline_abc123..."
        Must contain only letters, numbers, and underscores.
        Default: faultline
        """
        return os.environ.get("SCHEMA_NAME_PREFIX", "faultline")

    # ================================================================
    # VECTOR DATABASE CONFIGURATION (Qdrant)
    # ================================================================

    @property
    def QDRANT_URL(self) -> str:
        """URL of the Qdrant vector database server.

        Used for semantic search and fact retrieval.
        Format: http://hostname:6333
        Default: http://qdrant:6333 (Docker service name)
        """
        return os.environ.get("QDRANT_URL", "http://qdrant:6333")

    @property
    def QDRANT_COLLECTION(self) -> str:
        """Name of the Qdrant collection for storing embeddings.

        Can be the same for all users (simple) or per-user (faultline-{user_id}).
        Default: faultline-test (development)
        """
        return os.environ.get("QDRANT_COLLECTION", "faultline-test")

    # ================================================================
    # REDIS CONFIGURATION (Optional Caching)
    # ================================================================

    @property
    def REDIS_URL(self) -> Optional[str]:
        """Optional Redis URL for caching and deduplication.

        If set, enables caching to reduce duplicate API calls.
        Format: redis://hostname:6379/0
        Default: Not set (caching disabled)
        """
        return os.environ.get("REDIS_URL", None)

    # ================================================================
    # AI/LLM CONFIGURATION
    # ================================================================

    @property
    def QWEN_API_URL(self) -> str:
        """Primary LLM API endpoint for AI text generation.

        Used for fact extraction, correction handling, and ontology inference.
        Format: http://hostname:port/v1/chat/completions
        Default: http://localhost:11434/v1/chat/completions
        """
        return os.environ.get(
            "QWEN_API_URL",
            "http://localhost:11434/v1/chat/completions"
        )

    @property
    def WGM_LLM_MODEL(self) -> str:
        """LLM model name for the WGM (validation/ontology) endpoint.

        Model identity is pure configuration — the VALUE lives ONLY in the
        environment (.env), never as a code literal. Authoritative resolution is
        src.api.llm_calls.LLMModels (per-operation, fail-loud on unset). This
        accessor just surfaces the raw env value; empty string means unset.
        """
        return os.environ.get("WGM_LLM_MODEL", "")

    @property
    def PATTERN_EXTRACTION_MODEL(self) -> str:
        """LLM model name for the deterministic pattern-extraction op.

        Formerly the misnamed CATEGORY_LLM_MODEL (category inference is
        deterministic; no model). Falls back to WGM_LLM_MODEL when unset.
        Authoritative resolution is LLMModels.get("PATTERN_EXTRACTION").
        """
        return os.environ.get("PATTERN_EXTRACTION_MODEL", "") or \
            os.environ.get("WGM_LLM_MODEL", "")

    @property
    def OPENWEBUI_URL(self) -> Optional[str]:
        """Optional OpenWebUI base URL (for testing/development).

        Fallback when QWEN_API_URL not available.
        Default: Not set
        """
        return os.environ.get("OPENWEBUI_URL", None)

    @property
    def OPENWEBUI_INTERNAL_URL(self) -> Optional[str]:
        """Internal URL for OpenWebUI (Docker-to-Docker communication).

        Used when running in Docker to communicate between containers.
        Default: Not set (uses OPENWEBUI_URL or auto-detection)
        """
        return os.environ.get("OPENWEBUI_INTERNAL_URL", None)

    # ================================================================
    # EMBEDDING CONFIGURATION
    # ================================================================

    @property
    def EMBEDDING_MODEL_VERSION(self) -> str:
        """Embedding model VERSION tag for semantic search (Redis cache-invalidation key).

        PURE CONFIG — read from env (no code literal); the shipped default lives in
        .env.example (``EMBEDDING_MODEL_VERSION``). Empty when unset.
        """
        return (os.environ.get("EMBEDDING_MODEL_VERSION") or "").strip()

    @property
    def EMBEDDING_CACHE_TTL(self) -> int:
        """How long (in seconds) to cache embeddings in Redis.

        Higher values = fewer embedding API calls, but stale data longer.
        Lower values = fresher data, but more API calls.
        Default: 86400 (1 day)
        """
        return int(os.environ.get("EMBEDDING_CACHE_TTL", "86400"))

    # ================================================================
    # RE-EMBEDDER BACKGROUND JOB CONFIGURATION
    # ================================================================

    @property
    def REEMBED_INTERVAL(self) -> int:
        """How often (in seconds) the background job runs.

        Manages fact promotion, embedding updates, and ontology learning.
        Default: 60 seconds
        """
        return int(os.environ.get("REEMBED_INTERVAL", "60"))

    @property
    def QDRANT_SYNC_CONFIDENCE_THRESHOLD(self) -> float:
        """Minimum confidence score (0.0-1.0) to sync facts to Qdrant.

        Lower values = more facts stored; higher = only high-confidence facts.
        Default: 0.0 (all facts)
        """
        return float(os.environ.get("QDRANT_SYNC_CONFIDENCE_THRESHOLD", "0.0"))

    # ================================================================
    # TIMEOUT CONFIGURATION
    # ================================================================

    @property
    def HTTPX_TIMEOUT(self) -> int:
        """How long (in seconds) to wait for HTTP responses.

        Used for LLM API calls and other HTTP requests.
        If the LLM is slow, increase this.
        Default: 10 seconds
        """
        return int(os.environ.get("HTTPX_TIMEOUT", "10"))

    @property
    def DB_TIMEOUT(self) -> int:
        """How long (in seconds) to wait for database responses.

        If database queries are slow, increase this.
        Default: 30 seconds
        """
        return int(os.environ.get("DB_TIMEOUT", "30"))

    @property
    def QDRANT_TIMEOUT(self) -> int:
        """How long (in seconds) to wait for Qdrant responses.

        If vector search is slow, increase this.
        Default: 10 seconds
        """
        return int(os.environ.get("QDRANT_TIMEOUT", "10"))

    # ================================================================
    # CONNECTION POOL CONFIGURATION
    # ================================================================

    @property
    def DB_POOL_SIZE(self) -> int:
        """Maximum number of simultaneous database connections.

        Higher = more concurrent queries, but more server resources used.
        Lower = fewer resources, but queries may queue up.
        Default: 10 connections
        """
        return int(os.environ.get("DB_POOL_SIZE", "10"))

    # ================================================================
    # RATE LIMITING
    # ================================================================

    @property
    def RATE_LIMIT_PER_MIN(self) -> int:
        """Maximum API requests per user per minute.

        Prevents single users from overwhelming the system.
        Default: 100 requests/minute
        """
        return int(os.environ.get("RATE_LIMIT_PER_MIN", "100"))

    # ================================================================
    # LOGGING CONFIGURATION
    # ================================================================

    @property
    def FAULTLINE_LOG_LEVEL(self) -> str:
        """Logging level for FaultLine (DEBUG, INFO, WARNING, ERROR, CRITICAL).

        DEBUG = very detailed output, INFO = normal, ERROR = only problems.
        Default: INFO
        """
        return os.environ.get("FAULTLINE_LOG_LEVEL", "INFO").upper()

    # ================================================================
    # API CONFIGURATION
    # ================================================================

    @property
    def FAULTLINE_API_URL(self) -> str:
        """Public URL of FaultLine API (for filter to call backend).

        Example: http://localhost:8000 or https://api.example.com
        Default: http://localhost:8000
        """
        return os.environ.get("FAULTLINE_API_URL", "http://localhost:8000").rstrip("/")

    # ================================================================
    # MISCELLANEOUS
    # ================================================================

    @property
    def LLM_API_KEY(self) -> str:
        """Optional API key for LLM authentication.

        Some LLM endpoints require authentication.
        Default: Empty (no authentication)
        """
        return os.environ.get("LLM_API_KEY", "")

    @property
    def DEBUG_LM_STUDIO_STATS(self) -> bool:
        """Enable debug logging for LM Studio statistics (development only).

        Logs detailed timing and performance information.
        Default: False
        """
        return os.environ.get("DEBUG_LM_STUDIO_STATS", "").lower() in ("true", "1", "yes")

    @property
    def OPENWEBUI_REVERSE_PROXY(self) -> Optional[str]:
        """Optional reverse proxy URL for OpenWebUI (advanced).

        Use if OpenWebUI is behind a proxy server.
        Default: Not set
        """
        return os.environ.get("OPENWEBUI_REVERSE_PROXY", None)


# Singleton instance for easy importing
settings = Settings()
