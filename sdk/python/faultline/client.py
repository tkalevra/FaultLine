"""FaultLine v1 client — a thin, dependency-free wrapper over the REST calls.

Uses only the standard library (``urllib``) so the client has ZERO install-time
dependencies. Every method is a thin wrapper: it builds the request, sends it, and
returns a typed result; the FaultLine engine does all the memory work server-side.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .errors import FaultLineError, error_for
from .models import ForgetResult, LearnResult, RecallResult, WriteResult

_DEFAULT_TIMEOUT = 60.0


class Client:
    """A FaultLine memory client.

    Args:
        api_key:   your bearer token (``flk_…`` account key or ``flu_…`` seat token).
                   May be omitted against a self-hosted, open (no-auth) server.
        base_url:  the FaultLine host, e.g. ``https://memory.example.com``.
        user_id:   default tenant/seat UUID sent as ``X-FaultLine-User-Id``. Optional
                   when using a ``flu_`` seat token (the token IS the identity).
        timeout:   per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "http://localhost:8002",
        user_id: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.timeout = timeout

    # ── transport ─────────────────────────────────────────────────────────────

    def _headers(self, user_id: Optional[str]) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        uid = user_id or self.user_id
        if uid:
            h["X-FaultLine-User-Id"] = uid
        return h

    def _request(self, method: str, path: str, *, body: Optional[dict] = None,
                 params: Optional[dict] = None, user_id: Optional[str] = None) -> dict:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(user_id))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected — url from FAULTLINE_API_URL env, never user input
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise self._to_error(exc) from None
        except urllib.error.URLError as exc:
            raise FaultLineError(f"connection failed: {exc.reason}", code="connection") from None

    @staticmethod
    def _to_error(exc: "urllib.error.HTTPError") -> FaultLineError:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            payload = {}
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return error_for(exc.code, err.get("code", ""), err.get("message", ""),
                             err.get("detail"))
        # Non-/v1-shaped body (e.g. FastAPI {"detail": ...}) — still map by status.
        message = payload.get("detail") if isinstance(payload, dict) else str(exc.reason)
        return error_for(exc.code, "", str(message or exc.reason))

    # ── memory verbs ──────────────────────────────────────────────────────────

    def remember(self, text: str, *, user_id: Optional[str] = None) -> WriteResult:
        """Store facts from a natural-language message (verbatim — no pre-extraction)."""
        body = self._request("POST", "/v1/memories",
                             body={"text": text}, user_id=user_id)
        return WriteResult.from_envelope(body)

    def ingest_document(self, text: str, *, source_ref: str = "", title: str = "",
                        user_id: Optional[str] = None) -> WriteResult:
        """Ingest long-form/document text (chunked + mined server-side)."""
        body = self._request("POST", "/v1/documents",
                             body={"text": text, "source_ref": source_ref, "title": title},
                             user_id=user_id)
        return WriteResult.from_envelope(body)

    def recall(self, query: str, *, user_id: Optional[str] = None) -> RecallResult:
        """Recall grounded memory as prose for a verbatim question/message."""
        body = self._request("POST", "/v1/recall",
                             body={"query": query}, user_id=user_id)
        return RecallResult.from_envelope(body)

    def correct(self, text: str, *, user_id: Optional[str] = None) -> WriteResult:
        """Correct or retract a fact in natural language (verbatim)."""
        body = self._request("POST", "/v1/corrections",
                             body={"text": text}, user_id=user_id)
        return WriteResult.from_envelope(body)

    def forget(self, subject: str, *, rel_type: Optional[str] = None,
               old_value: Optional[str] = None, user_id: Optional[str] = None) -> ForgetResult:
        """Tombstone exactly ONE named fact (bounded — never a bulk wipe)."""
        body = self._request("DELETE", "/v1/memories",
                             body={"subject": subject, "rel_type": rel_type,
                                   "old_value": old_value},
                             user_id=user_id)
        return ForgetResult.from_envelope(body)

    def learn(self, text: str, *, user_id: Optional[str] = None) -> LearnResult:
        """Learn ontological statements (one per line) or run an ``/expand <topic>`` command."""
        body = self._request("POST", "/v1/learn",
                             body={"text": text}, user_id=user_id)
        return LearnResult.from_envelope(body)

    def health(self) -> dict:
        """Liveness + version (no auth required)."""
        return self._request("GET", "/v1/health")
