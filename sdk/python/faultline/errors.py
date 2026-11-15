"""Typed errors for the FaultLine client. Mirrors the v1 error envelope:

    {"error": {"code": "...", "message": "...", "detail": {...}}}
"""

from __future__ import annotations

from typing import Any, Optional


class FaultLineError(Exception):
    """Base error. Carries the HTTP status, the API error ``code``, and any ``detail``."""

    def __init__(self, message: str, *, status: int = 0, code: str = "",
                 detail: Optional[Any] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        return f"[{self.status} {self.code}] {base}" if self.status else base


class AuthError(FaultLineError):
    """401 — missing/invalid bearer token."""


class ForbiddenError(FaultLineError):
    """403 — cross-account/BOLA, tenant spoof, or origin not allowed."""


class BadRequestError(FaultLineError):
    """400 — malformed/absent user_id or a missing required field."""


class QuotaExceededError(FaultLineError):
    """402 — a hard usage cap for this seat's plan has been reached."""


class ReadOnlyError(FaultLineError):
    """409 — the seat is in agent 'serve' mode (writes frozen); recall still works."""


class RateLimitedError(FaultLineError):
    """429 — per-account request rate limit exceeded. Retry after a moment."""


class ProvisioningError(FaultLineError):
    """503 — the tenant's memory is still being set up. Retry shortly."""


_STATUS_ERROR = {
    400: BadRequestError,
    401: AuthError,
    402: QuotaExceededError,
    403: ForbiddenError,
    409: ReadOnlyError,
    429: RateLimitedError,
    503: ProvisioningError,
}


def error_for(status: int, code: str, message: str, detail: Any = None) -> FaultLineError:
    """Pick the most specific error class for an HTTP status / API code."""
    cls = _STATUS_ERROR.get(status, FaultLineError)
    return cls(message or code or f"HTTP {status}", status=status, code=code, detail=detail)
