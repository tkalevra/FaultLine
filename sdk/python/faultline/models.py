"""Lightweight typed result wrappers over the v1 JSON envelopes.

Each wraps the raw response dict and lifts the common fields. The full server payload
is always available on ``.data`` (the exact dict an MCP client would receive), so the
client never hides anything the engine returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class WriteResult:
    """Result of remember / document ingest."""

    status: Optional[str]
    committed: Optional[int]
    staged: Optional[int]
    message: Optional[str]
    data: dict = field(default_factory=dict)

    @classmethod
    def from_envelope(cls, body: dict) -> "WriteResult":
        return cls(
            status=body.get("status"),
            committed=body.get("committed"),
            staged=body.get("staged"),
            message=body.get("message"),
            data=body.get("data", body),
        )


@dataclass
class RecallResult:
    """Result of recall — grounded prose plus the raw payload."""

    query: Optional[str]
    memory: Optional[str]
    data: dict = field(default_factory=dict)

    @classmethod
    def from_envelope(cls, body: dict) -> "RecallResult":
        return cls(query=body.get("query"), memory=body.get("memory"),
                   data=body.get("data", body))

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.memory or ""


@dataclass
class ForgetResult:
    status: Optional[str]
    message: Optional[str]
    data: dict = field(default_factory=dict)

    @classmethod
    def from_envelope(cls, body: dict) -> "ForgetResult":
        return cls(status=body.get("status"), message=body.get("message"),
                   data=body.get("data", body))


@dataclass
class LearnResult:
    status: Optional[str]
    total: Optional[int]
    message: Optional[str]
    data: dict = field(default_factory=dict)

    @classmethod
    def from_envelope(cls, body: dict) -> "LearnResult":
        return cls(status=body.get("status"), total=body.get("total"),
                   message=body.get("message"), data=body.get("data", body))
