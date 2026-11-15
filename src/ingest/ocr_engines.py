"""Engine-pluggable OCR uplift for the store-first corpus lane (migration 327).

THE DESIGN (owner): pixel formats (pdf-with-image-pages, png/jpg/…) store the
SNAPSHOT at ingest with layout_text EMPTY and status='incomplete'; the text
arrives later through an OCR uplift behind this interface — never on the ingest
path, never on the serve path.

THE EXTERNAL CONTRACT implemented by :class:`DeepSeekOCREngine` is the DeepSeek
OCR tool API (https://api-docs.deepseek.com/guides/ocr/, verified 2026-11-14):

* ``POST https://api.deepseek.com/v1/tools/ocr`` — multipart/form-data with
  EXACTLY two fields: ``file`` (the bytes, WITH a filename — format is detected
  from actual content) and ``purpose`` ('text_recognition' here: Word/PPT
  without images extract text directly, everything else is recognized page by
  page — which is exactly this lane's semantics);
* the response is ONE JSON object, possibly preceded by newline keep-alive
  whitespace — the whole body parses as JSON after a strip;
* ``pages[]`` each carry ``index`` (0-based), ``text`` (raw model output, no
  cleanup) and ``finish_reason`` ('stop' | 'length'); 'length' means the page
  text is INCOMPLETE — the zero-loss gate in corpus_store.pages_status turns
  any such page into an honest status='incomplete';
* layout anchors such as ``<｜ref｜>…<｜/ref｜><｜box｜>[[x1,y1,x2,y2]]<｜/box｜>``
  stay IN the text (coordinates normalized 0–1000); we store them verbatim and
  never strip them;
* an ``{"error": {...}}`` body (HTTP 200 or otherwise) is a terminal refusal
  for THAT attempt (password-protected file, undecodable image, >1000 pages,
  >100MiB) or a transient one (busy / gateway timeout) — mapped below to
  :class:`OCRError` with ``transient`` set so a future scheduler can retry
  bounded, never loop.

NO KEY IN CODE: the caller supplies ``api_key``; :func:`get_ocr_engine` reads
``DEEPSEEK_API_KEY`` from the environment. The engine selection itself is
``OCR_ENGINE`` (``none`` | ``deepseek``; default ``none``).
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass
from typing import Any, Optional

DEEPSEEK_OCR_URL_DEFAULT = "https://api.deepseek.com/v1/tools/ocr"

#: Non-retryable error messages from the guide's error table.
_TERMINAL_ERROR_FRAGMENTS = (
    "password",
    "cannot be parsed",
    "format is not supported",
    "could not be decoded",
    "more than 1000 pages",
    "max 100 MiB",
    "file is too large",
)


class OCRUnavailable(RuntimeError):
    """No engine configured (OCR_ENGINE=none) — the uplift is not wired."""


class OCRError(RuntimeError):
    """An engine attempt failed. ``transient`` says whether a bounded retry
    could ever help (busy / gateway timeout) or the file itself is refused
    (password, undecodable, too large)."""

    def __init__(self, message: str, *, transient: bool = False,
                 status: Optional[int] = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


@dataclass
class OCRPage:
    """One recognized page — the guide's pages[] row, verbatim semantics."""
    page_index: int
    text: str
    finish_reason: Optional[str]  # 'stop' | 'length' | None (absent in body)


class OCREngine(abc.ABC):
    """The pluggable interface. ``name`` is stamped on corpus_documents.ocr_engine."""

    name: str = "abstract"

    @abc.abstractmethod
    async def recognize(self, data: bytes, filename: str = "") -> list[OCRPage]:
        """Recognize ``data`` page by page. Raises OCRError/OCRUnavailable —
        never returns fabricated pages."""


class NoneOCREngine(OCREngine):
    """The DEFAULT engine (OCR_ENGINE=none): no OCR anywhere. recognize() always
    raises OCRUnavailable — the corpus rows stay status='incomplete' with
    pending pages, honestly, until an engine is configured."""

    name = "none"

    async def recognize(self, data: bytes, filename: str = "") -> list[OCRPage]:
        raise OCRUnavailable(
            "no OCR engine configured (OCR_ENGINE=none) — the snapshot is "
            "retained; layout_text stays pending until an engine is configured")


class DeepSeekOCREngine(OCREngine):
    """The DeepSeek OCR tool caller (multipart, purpose=text_recognition)."""

    name = "deepseek"

    def __init__(self, api_key: str, url: str = DEEPSEEK_OCR_URL_DEFAULT,
                 timeout_s: float = 185.0):
        if not (api_key or "").strip():
            raise OCRError("DeepSeek OCR requires an API key "
                           "(env DEEPSEEK_API_KEY) — none configured")
        self.api_key = api_key.strip()
        self.url = url or DEEPSEEK_OCR_URL_DEFAULT
        # The guide's total wall is 180s; +5s is transport margin. The caller
        # (a background uplift worker) owns any retry cadence — bounded, never
        # a loop.
        self.timeout_s = float(timeout_s)

    async def recognize(self, data: bytes, filename: str = "") -> list[OCRPage]:
        import httpx

        files = {"file": ((filename or "document"), data)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    self.url,
                    data={"purpose": "text_recognition"},
                    files=files,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as exc:
            raise OCRError(f"deepseek ocr transport failure: {exc!r}",
                           transient=True) from exc

        body: dict[str, Any]
        try:
            body = resp.json()
        except ValueError as exc:
            raise OCRError(
                f"deepseek ocr returned a non-JSON body "
                f"(status={resp.status_code})", transient=True) from exc

        if isinstance(body.get("error"), dict):
            message = str(body["error"].get("message") or "unknown error")
            raise OCRError(
                f"deepseek ocr refused the file: {message}",
                transient=not any(f in message.lower()
                                  for f in _TERMINAL_ERROR_FRAGMENTS),
                status=resp.status_code)
        if resp.status_code != 200:
            raise OCRError(f"deepseek ocr http {resp.status_code}",
                           transient=True, status=resp.status_code)

        pages: list[OCRPage] = []
        for row in body.get("pages") or []:
            try:
                pages.append(OCRPage(
                    page_index=int(row.get("index")),
                    text=str(row.get("text") or ""),  # raw model output, VERBATIM
                    finish_reason=row.get("finish_reason"),
                ))
            except (TypeError, ValueError) as exc:
                raise OCRError(f"deepseek ocr malformed page row: {row!r:.200}",
                               transient=False) from exc
        if not pages:
            raise OCRError("deepseek ocr returned no pages", transient=True)
        return pages


def get_ocr_engine(env: Optional[dict[str, str]] = None) -> OCREngine:
    """Select the engine from OCR_ENGINE (default 'none'). Unknown names raise
    OCRError at CONFIGURATION time — better than silently running none."""
    _env = env if env is not None else dict(os.environ)
    name = (_env.get("OCR_ENGINE") or "none").strip().lower()
    if name == "none":
        return NoneOCREngine()
    if name == "deepseek":
        return DeepSeekOCREngine(_env.get("DEEPSEEK_API_KEY") or "",
                                 _env.get("DEEPSEEK_OCR_URL")
                                 or DEEPSEEK_OCR_URL_DEFAULT)
    raise OCRError(f"unknown OCR_ENGINE: {name!r} (known: none, deepseek)")
