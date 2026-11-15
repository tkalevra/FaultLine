"""
title: FaultLine Attachment Bridge
author: tkalevra
version: 0.1.0
required_open_webui_version: 0.9.0
requirements: httpx
"""

# ══════════════════════════════════════════════════════════════════════════════════════
# SERVER-SIDE ATTACHMENT BRIDGE — the OWUI half of the EPA fix (2026-10-22).
#
# THE WOUND (DEV/RESEARCH/epa-ingest-validation-20261022.md): when a user attaches a
# file, OpenWebUI's RAG pipeline extracts text and shows the MODEL only <source> chunks —
# never the bytes. A model asked to "remember this doc" could only hand-stitch ~16K chars
# of extracted text into a giant ingest_document argument (one model burned 59K chars of
# thinking on it and was STOP-killed mid-turn).
#
# OWNER DIRECTIVE: FaultLine does the parsing work server-side; users NEVER wait for
# their local AI to parse/re-transmit documents. This filter, installed in OpenWebUI,
# POSTs every freshly-attached file to the FaultLine attachment door
# (``POST /ingest/attachment``) AT ATTACH TIME — server-side parsing, artefact retention,
# stub facts — and annotates the message so the model SEES that ingestion already
# started. The model's job shrinks to a status check (``document_status``); it must
# never re-transmit a document.
#
# ⚠️ RELATIONSHIP TO THE LEGACY FILTER — READ BEFORE CHANGING THIS ASSESSMENT.
# `faultline_function.py` (the old WGM inlet/outlet filter) is DISABLED BY DESIGN —
# CLAUDE.md: "Not a bug. Do not flag is_active=False." The MCP tool server is the live
# path; the old filter's extract-on-every-message behavior is intentionally retired.
# THIS FILE IS AN ADDITION FOR ATTACHMENTS ONLY, not a re-enable of that behavior:
# it performs NO memory extraction, NO recall injection, and NO outlet processing.
# Its only act is the attachment→bridge POST plus the annotation. Enabling it changes
# nothing about the legacy filter's status.
#
# AUTH: the FAULTLINE_API_KEY valve must hold the SAME bearer the OWUI tool-server
# connection uses (the TOOL-SERVER credential — MCP_API_KEY in the open core, the
# account key under SaaS). The seat identity rides X-OpenWebUI-User-Id, stamped here
# from the signed-in OWUI user. This is middleware-trusted server-to-server auth — the
# OWUI platform acting FOR its user — distinct from per-seat flu_ tokens.
#
# FAIL-SAFE: every failure logs and returns the body UNCHANGED. A broken bridge must
# never break a chat — the model can still save a document the old way if it must.
# ══════════════════════════════════════════════════════════════════════════════════════

import base64
import hashlib
import sys
import time
from typing import Optional

import httpx
from pydantic import BaseModel, Field


def _log(msg: str, debug_only: bool = False, debug: bool = False):
    if debug_only and not debug:
        return
    print(f"[FaultLine Attachment Bridge] {msg}", file=sys.stderr, flush=True)


#: Hard cap on the in-process dedup map — the TTL prunes staleness but cannot bound a
#: flood of distinct files; this keeps the filter's memory bounded under abuse.
_MAX_SEEN_ENTRIES = 10000


class Filter:
    """Bridges freshly-attached files to FaultLine's server-side ingest door."""

    class Valves(BaseModel):
        FAULTLINE_MCP_URL: str = Field(
            default="",
            description=("Base URL of the FaultLine MCP server (port 8002), e.g. "
                         "http://faultline-mcp:8002 (Docker) or https://mcp.example-host.com. "
                         "REQUIRED — the bridge is disabled while empty.")
        )
        FAULTLINE_API_KEY: str = Field(
            default="",
            description=("The TOOL-SERVER bearer credential — the SAME key configured on "
                         "OpenWebUI's tool-server connection for FaultLine (MCP_API_KEY in "
                         "the open core; the account key under SaaS). NOT a per-seat token. "
                         "REQUIRED — the bridge is disabled while empty.")
        )
        OWUI_FILES_BASE_URL: str = Field(
            default="http://localhost:8080",
            description=("Origin used to resolve relative file URLs (/api/v1/files/...). "
                         "Inside Docker: http://open-webui:8080. The filter runs inside the "
                         "OWUI backend, so localhost is usually correct.")
        )
        ENABLED: bool = Field(default=True, description="Master switch for the bridge.")
        BRIDGE_PATH: str = Field(
            default="/ingest/attachment",
            description="FaultLine door the filter POSTs to. Leave the default."
        )
        TIMEOUT_SECS: float = Field(
            default=30.0,
            description="Seconds to wait for the bridge POST before giving up (the ingest "
                        "itself continues server-side regardless)."
        )
        FETCH_TIMEOUT_SECS: float = Field(
            default=15.0, description="Seconds to wait fetching the file's bytes from OWUI."
        )
        MAX_FILES_PER_MESSAGE: int = Field(
            default=5, description="Cap on files bridged per message (flood guard)."
        )
        DEDUP_TTL_SECS: float = Field(
            default=21600.0,
            description=("OWUI may re-fire the inlet for the same message (regenerate, "
                         "streaming). Same (user, file hash) within this window is NOT "
                         "re-POSTed; the annotation reuses the cached document id.")
        )
        ENABLE_DEBUG: bool = Field(
            default=False, description="Verbose logging to the OWUI container log."
        )

    def __init__(self):
        self.valves = self.Valves()
        # (user_id, sha256) -> (monotonic ts, receipt dict). In-process is sufficient:
        # the bridge itself dedups retained artefacts by content hash server-side, so a
        # multi-worker OWUI worst case is a duplicate POST that lands as `duplicate: true`.
        self._seen: dict = {}

    # ── OWUI file-entry accessors (shape varies across OWUI versions — read BOTH) ────

    @staticmethod
    def _entry_name(entry: dict) -> str:
        f = entry.get("file") if isinstance(entry.get("file"), dict) else {}
        return (entry.get("name") or entry.get("filename")
                or f.get("name") or f.get("filename") or "attachment")

    @staticmethod
    def _entry_url(entry: dict) -> str:
        f = entry.get("file") if isinstance(entry.get("file"), dict) else {}
        return entry.get("url") or f.get("url") or ""

    @staticmethod
    def _entry_context(entry: dict) -> str:
        f = entry.get("file") if isinstance(entry.get("file"), dict) else {}
        c = entry.get("context") or f.get("content") or ""
        return c if isinstance(c, str) else ""

    # ── byte resolution: data URI → OWUI files API → local path → text fallback ─────

    def _b64_from_data_uri(self, url: str) -> Optional[str]:
        if not url.startswith("data:"):
            return None
        try:
            head, _, payload = url.partition(",")
            if ";base64" not in head:
                return None
            return payload
        except Exception as e:  # noqa: BLE001 — fail-safe, never break the chat
            _log(f"data-uri decode failed: {e}", debug_only=True, debug=self.valves.ENABLE_DEBUG)
            return None

    def _b64_from_owui_files_api(self, url: str, user_token: str) -> Optional[str]:
        """GET the ORIGINAL bytes back from OpenWebUI's own files API.

        The RAG pipeline keeps the upload; /api/v1/files/{id}/file serves it. This needs
        the signed-in user's token (OWUI passes it to filters as __user__.token) — the
        filter is platform code acting FOR the user, so their own credential is the
        correct one. No token (API-key-only deployments) → we cannot fetch → None.

        SSRF/credential-forwarding guard: the user's bearer is sent to the OWUI origin
        ONLY. Relative /api/v1/files/... paths resolve against OWUI_FILES_BASE_URL; an
        ABSOLUTE url is honored only when it IS that origin. Anything else is refused —
        a crafted chat body must never point this fetch (and its Authorization header)
        at a third-party host."""
        if not url:
            return None
        base = self.valves.OWUI_FILES_BASE_URL.rstrip("/")
        if url.startswith("/api/v1/files/"):
            if not user_token:
                _log("file URL is OWUI-relative but no user token available — cannot fetch bytes",
                     debug_only=True, debug=self.valves.ENABLE_DEBUG)
                return None
            full = base + url
        elif url.startswith(base + "/"):
            if not user_token:
                return None
            full = url
        else:
            _log(f"refusing non-OWUI file url (SSRF guard): {url[:80]}", debug_only=True,
                 debug=self.valves.ENABLE_DEBUG)
            return None
        try:
            # follow_redirects=False: httpx forwards the Authorization header on redirect,
            # and a redirect must never carry the user's token off-origin.
            resp = httpx.get(full, headers={"Authorization": f"Bearer {user_token}"},
                             timeout=self.valves.FETCH_TIMEOUT_SECS, follow_redirects=False)
            if resp.status_code != 200:
                _log(f"files API fetch status={resp.status_code}", debug_only=True,
                     debug=self.valves.ENABLE_DEBUG)
                return None
            return base64.b64encode(resp.content).decode("ascii")
        except Exception as e:  # noqa: BLE001
            _log(f"files API fetch failed: {e}", debug_only=True, debug=self.valves.ENABLE_DEBUG)
            return None

    def _b64_from_local_path(self, entry: dict) -> Optional[str]:
        path = entry.get("path") or ""
        if not path or not isinstance(path, str):
            return None
        try:
            import os
            if not os.path.isfile(path):
                return None
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode("ascii")
        except Exception as e:  # noqa: BLE001
            _log(f"local path read failed ({path[:80]}): {e}", debug_only=True,
                 debug=self.valves.ENABLE_DEBUG)
            return None

    # ── the bridge call ───────────────────────────────────────────────────────────────

    def _post_bridge(self, user_id: str, filename: str, data_b64: str,
                     source_ref: str = "") -> Optional[dict]:
        """POST one attachment to the FaultLine bridge door. Returns the tool-result dict
        or None on any failure (logged). Synchronous on purpose: OWUI filters run the
        inlet on the event loop, and httpx's sync API inside a worker keeps this simple
        and bounded by TIMEOUT_SECS."""
        url = self.valves.FAULTLINE_MCP_URL.rstrip("/") + self.valves.BRIDGE_PATH
        try:
            resp = httpx.post(
                url,
                json={"filename": filename, "data_b64": data_b64, "source_ref": source_ref},
                headers={
                    "Authorization": f"Bearer {self.valves.FAULTLINE_API_KEY}",
                    "X-OpenWebUI-User-Id": user_id,
                },
                timeout=self.valves.TIMEOUT_SECS,
            )
            if resp.status_code == 429:
                _log(f"bridge rate-limited (429) for {user_id[:8]}... — file NOT queued; "
                     "the model can still ingest it the old way")
                return None
            if resp.status_code in (401, 403):
                _log(f"bridge auth failed ({resp.status_code}) — check FAULTLINE_API_KEY "
                     "matches the tool-server connection credential")
                return None
            if resp.status_code != 200:
                _log(f"bridge returned {resp.status_code} — file NOT queued")
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001 — the chat must survive a dead bridge
            _log(f"bridge POST failed: {e}")
            return None

    def _post_text_fallback(self, user_id: str, filename: str, text: str) -> Optional[dict]:
        """Bytes unavailable but OWUI's extracted text exists: hand THAT to the server
        (existing ingest_document door) so parsing STILL happens server-side and the
        model still never re-transmits anything."""
        url = self.valves.FAULTLINE_MCP_URL.rstrip("/") + "/ingest_document"
        try:
            resp = httpx.post(
                url,
                json={"filename": filename, "text": text, "source_ref": filename},
                headers={
                    "Authorization": f"Bearer {self.valves.FAULTLINE_API_KEY}",
                    "X-OpenWebUI-User-Id": user_id,
                },
                timeout=self.valves.TIMEOUT_SECS,
            )
            if resp.status_code != 200:
                _log(f"text fallback returned {resp.status_code}", debug_only=True,
                     debug=self.valves.ENABLE_DEBUG)
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            _log(f"text fallback failed: {e}", debug_only=True, debug=self.valves.ENABLE_DEBUG)
            return None

    # ── receipt → model-facing annotation ─────────────────────────────────────────────

    @staticmethod
    def _safe_label(name: str, cap: int = 80) -> str:
        """A filename is USER-CONTROLLED and is about to be interpolated into text that
        the MODEL reads as system-ish context. Collapse whitespace/control characters
        (a newline would FORGE additional annotation lines) and cap the length.
        U+2028/U+2029/U+0085 count as line breaks even though str.isprintable() calls
        them printable."""
        _LINEBREAKS = "\u2028\u2029\u0085"
        cleaned = "".join(
            ch if (ch.isprintable() and not ch.isspace() and ch not in _LINEBREAKS) or ch == " "
            else " " for ch in str(name))
        return cleaned[:cap].strip() or "attachment"

    @staticmethod
    def _receipt_annotation(filename: str, receipt: dict) -> Optional[str]:
        """One line the model reads: ingestion already started; job = status check."""
        doc = receipt.get("document") or {}
        doc_id = (receipt.get("bridge") or {}).get("document_id") or doc.get("document_id")
        safe_name = Filter._safe_label(filename)
        if receipt.get("status") in ("rejected", "invalid_request", "error"):
            # Honest: the bridge refused (unsupported type / bad b64 / oversized). Tell
            # the model exactly once, so it can tell the user instead of retrying blind.
            return (f"[FaultLine] Attachment '{safe_name}' could not be auto-ingested "
                    f"({receipt.get('status')}). You may ingest it manually if the user asks.")
        if doc_id is None:
            return None
        eta = doc.get("eta_seconds")
        eta_txt = f", ETA ~{int(eta)}s" if isinstance(eta, (int, float)) else ""
        return (f"FaultLine is already ingesting this document (id {doc_id}{eta_txt}); "
                f"check progress with document_status — never re-transmit the document's "
                f"contents.")

    def _dedup_check(self, user_id: str, fingerprint: str):
        now = time.monotonic()
        ttl = self.valves.DEDUP_TTL_SECS
        self._seen = {k: v for k, v in self._seen.items() if now - v[0] < ttl}
        if len(self._seen) >= _MAX_SEEN_ENTRIES:   # hard cap: TTL alone cannot bound a flood
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1][0])[:64]:
                self._seen.pop(k, None)
        hit = self._seen.get((user_id, fingerprint))
        return hit[1] if hit else None

    # ── inlet / outlet ────────────────────────────────────────────────────────────────

    async def inlet(self, body: dict, __user__: dict = None) -> dict:
        try:
            if not self.valves.ENABLED:
                return body
            if not self.valves.FAULTLINE_MCP_URL or not self.valves.FAULTLINE_API_KEY:
                _log("disabled: FAULTLINE_MCP_URL / FAULTLINE_API_KEY valves not set",
                     debug_only=True, debug=self.valves.ENABLE_DEBUG)
                return body
            files = body.get("files") or []
            if not isinstance(files, list) or not files:
                return body
            user_id = str((__user__ or {}).get("id") or "").strip()
            if not user_id:
                _log("no __user__.id — cannot name the seat; skipping bridge",
                     debug_only=True, debug=self.valves.ENABLE_DEBUG)
                return body
            user_token = str((__user__ or {}).get("token") or "")

            notes: list[str] = []
            bridged = 0
            for entry in files:
                if bridged >= self.valves.MAX_FILES_PER_MESSAGE:
                    break
                if not isinstance(entry, dict) or entry.get("_faultline_bridged"):
                    continue
                filename = self._entry_name(entry)
                url = self._entry_url(entry)
                data_b64 = (self._b64_from_data_uri(url)
                            or self._b64_from_owui_files_api(url, user_token)
                            or self._b64_from_local_path(entry))
                if data_b64:
                    fingerprint = hashlib.sha256(base64.b64decode(data_b64)).hexdigest()
                    cached = self._dedup_check(user_id, fingerprint)
                    if cached is not None:
                        receipt = cached
                        _log(f"dedup: {filename[:60]} already bridged", debug_only=True,
                             debug=self.valves.ENABLE_DEBUG)
                    else:
                        receipt = self._post_bridge(user_id, filename, data_b64,
                                                    source_ref=filename)
                        if receipt is not None:
                            self._seen[(user_id, fingerprint)] = (time.monotonic(), receipt)
                else:
                    # Bytes unavailable — fall back to the extracted text OWUI already
                    # holds, still parsed SERVER-side (owner directive: the local AI
                    # never parses/re-transmits documents).
                    text = self._entry_context(entry)
                    if not text.strip():
                        entry["_faultline_bridged"] = True   # nothing resolvable; mark once
                        _log(f"no bytes and no extracted text for {filename[:60]} — skipped",
                             debug_only=True, debug=self.valves.ENABLE_DEBUG)
                        continue
                    fingerprint = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
                    cached = self._dedup_check(user_id, fingerprint)
                    receipt = cached if cached is not None else self._post_text_fallback(
                        user_id, filename, text)
                    if receipt is not None:
                        self._seen[(user_id, fingerprint)] = (time.monotonic(), receipt)

                entry["_faultline_bridged"] = True
                note = self._receipt_annotation(filename, receipt or {})
                if note:
                    notes.append(f"Attachment '{self._safe_label(filename)}': {note}")
                    bridged += 1

            if notes:
                block = ("[FaultLine attachment bridge] " + " ".join(notes))
                # (1) Into the file entries' RAG context (OWUI injects it as <source>).
                for entry in files:
                    if isinstance(entry, dict) and entry.get("_faultline_bridged"):
                        ctx = self._entry_context(entry)
                        if ctx and "FaultLine attachment bridge" not in ctx:
                            if isinstance(entry.get("file"), dict):
                                entry["file"]["content"] = (
                                    ctx + "\n\n" + block)
                            else:
                                entry["context"] = ctx + "\n\n" + block
                # (2) Onto the last user message, so the model sees it even when RAG
                #     context injection is disabled for the file type.
                msgs = body.get("messages") or []
                for m in reversed(msgs):
                    if m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str) and block not in c:
                            m["content"] = c + "\n\n" + block
                        elif isinstance(c, list):
                            if not any(isinstance(p, dict) and block in (p.get("text") or "")
                                       for p in c):
                                c.append({"type": "text", "text": block})
                        break
                _log(f"bridged {bridged} attachment(s) for {user_id[:8]}...")
            return body
        except Exception as e:  # noqa: BLE001 — THE CHAT MUST SURVIVE THE BRIDGE
            _log(f"inlet failed (body passed through unchanged): {e}")
            return body

    async def outlet(self, body: dict, __user__: dict = None) -> dict:
        # Deliberately inert: this filter adds attachments-bridging only. Memory
        # extraction/recall belongs to the MCP tools, not to an OWUI filter.
        return body
