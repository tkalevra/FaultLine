"""THE ONE ERROR SEAM — what a caller may read about a failure, and what only the log may.

Every HTTP route that used to hand ``str(exc)`` / ``repr(exc)`` / ``f"…{exc}"`` to a response
body now goes through this module. There are exactly three things a failure can be, and the
seam decides which BY THE EXCEPTION'S TYPE, never by the call site's mood:

1. **UNEXPECTED** (anything not covered below): the caller gets a generic, authored message
   plus a *correlation id*; the log gets the full exception text and traceback under that
   same id. Nothing about our internals — file paths, SQL, driver messages, upstream URLs
   carrying ``?api-key=`` — reaches a client. This is OWASP ASVS 4.0 **V7.4.1** ("a generic
   message is shown when an unexpected or security sensitive error occurs, potentially with a
   unique ID which support personnel can use to investigate") and **V14.4** / CWE-209
   (error messages must not expose sensitive information such as stack traces).
2. **REFUSAL** (``PublicRefusal`` subclasses, or a type registered with
   ``register_refusal_type``): a decision WE made and WORDED — "bucket must be an integer",
   "endpoint URL rejected: private address". Its message is authored, constant-shaped and
   meant for the caller, so it passes through verbatim. This is what separates a 4xx
   validation result from a 5xx fault; conflating them would turn every actionable refusal
   into "internal error (ref …)" and send tenants to support for their own typo.
3. **PROVIDER DIAGNOSTIC**: the words a configured endpoint answered with. Redacted by
   the caller before it travels; never passed through here raw.

WHY THE TYPE, NOT AN ARGUMENT. A ``disclose=True`` flag on the seam is a per-site opt-out and
the first copy-paste of one would be the leak this seam exists to close. A refusal is a
refusal because the code that RAISED it wrote the sentence — that fact lives on the class.

CODEQL NOTE (py/stack-trace-exposure, verified against
``semmle/python/dataflow/new/internal/TaintTrackingPrivate.qll`` in python-all 7.2.5): taint
rides ``str(exc)``, f-strings, concatenation and container stores; it does NOT ride an
attribute read. ``PublicRefusal.public_message`` is therefore deliberately an attribute
populated at ``__init__`` from the authored argument, and this seam returns that attribute
rather than ``str(exc)`` — so a route that passes a refusal through is provably clean to the
analyser for the same reason it is clean to a reader.

LOGGING. ``log_crit(where, correlation_id, exc, tb)`` is the single sink: one stdlib
``logging`` CRITICAL record on the ``faultline.errors`` logger (the API's structlog is bound to
the stdlib ``LoggerFactory``, so this lands in the same handlers as ``log_crit`` in
``logging_config.py``; the MCP process logs the same way). The message and traceback pass
through ``_LOG_REDACTOR`` first. The engine default is the identity; a deployment that
knows credential shapes installs a real redactor via ``set_log_redactor`` at start-up, and
ASVS **V7.1.1** ("does not log credentials or payment details") is what that install is for.
"""

from __future__ import annotations

import logging
import secrets
import traceback as _traceback
from typing import Any, Callable, Optional

__all__ = [
    "GENERIC_ERROR",
    "PublicRefusal",
    "register_refusal_type",
    "is_refusal",
    "new_correlation_id",
    "log_crit",
    "log_provider_sentence",
    "public_error",
    "public_detail",
    "set_log_redactor",
    "redact_text",
    "redactor_installed",
]

#: The authored, generic message a caller sees for an UNEXPECTED failure. One string, one
#: place — a test pins that no response body carries exception text and every one carries this
#: (or a ``what`` the route authored) beside a correlation id.
GENERIC_ERROR = "internal error"

_LOGGER_NAME = "faultline.errors"
_log = logging.getLogger(_LOGGER_NAME)

#: Traceback bytes kept in the log record. A full trace is the point; the cap only stops a
#: pathological recursion from writing megabytes per request.
_TRACEBACK_CAP = 16_000


class PublicRefusal(Exception):
    """A refusal WE authored — its message is meant for the caller and passes through the seam.

    Subclass this (usually as a mixin beside ``ValueError``) for a typed, deliberately-raised
    decision whose text the raiser wrote: ``raise SignatureRequired("… requires a typed-name
    signature")``. The message is captured into ``public_message`` at construction; that is the
    attribute the seam returns (see the CodeQL note in the module docstring).
    """

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        first = args[0] if args else ""
        self.public_message: str = first if isinstance(first, str) else GENERIC_ERROR


# Types that are refusals by contract but could not be made ``PublicRefusal`` subclasses at the
# time — a module frozen by another lane, or a third-party class. Registered by the module that
# OWNS the type, never at a call site.
_REFUSAL_TYPES: tuple[type, ...] = ()


def register_refusal_type(cls: type) -> None:
    """Declare ``cls`` (an Exception subclass) a refusal whose message is authored and public."""
    global _REFUSAL_TYPES
    if not (isinstance(cls, type) and issubclass(cls, BaseException)):
        raise TypeError(f"register_refusal_type expects an exception class, got {cls!r}")
    if cls not in _REFUSAL_TYPES:
        _REFUSAL_TYPES = _REFUSAL_TYPES + (cls,)


def is_refusal(exc: BaseException) -> bool:
    return isinstance(exc, PublicRefusal) or (bool(_REFUSAL_TYPES) and isinstance(exc, _REFUSAL_TYPES))


def _refusal_message(exc: BaseException) -> str:
    """The authored sentence of a refusal — read as an ATTRIBUTE / ``args`` element, never as
    ``str(exc)``, so the analyser sees what the reader sees: an authored constant, not a
    formatted exception."""
    msg = getattr(exc, "public_message", None)
    if isinstance(msg, str) and msg:
        return msg
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], str) and args[0]:
        return args[0]
    return GENERIC_ERROR


def new_correlation_id() -> str:
    """12 hex chars — enough to be unique per incident, short enough to read off a screen."""
    return secrets.token_hex(6)


# ── the log sink ─────────────────────────────────────────────────────────────────────

def _IDENTITY(s: str) -> str:  # the default: nothing deployment-specific to know about
    return s


_LOG_REDACTOR: Callable[[str], str] = _IDENTITY


def set_log_redactor(fn: Callable[[str], str]) -> None:
    """Install the text redactor every ``log_crit`` line passes through."""
    global _LOG_REDACTOR
    if not callable(fn):
        raise TypeError("log redactor must be callable")
    _LOG_REDACTOR = fn


def redactor_installed() -> bool:
    """True once a real redactor (not the engine's identity default) is bound. A deployment
    binds it EXPLICITLY at start-up: relying on an import side-effect elsewhere leaves the
    identity lambda in place in processes that never ran the installer."""
    return _LOG_REDACTOR is not _IDENTITY


def _safe_redact(text: str) -> str:
    try:
        return _LOG_REDACTOR(text)
    except Exception:  # noqa: BLE001 — a redactor fault must not lose the incident record …
        # … but it must not leak either: if the redactor is broken, log the SHAPE only.
        return f"<unredactable text, {len(text)} chars>"


def redact_text(text: Optional[str]) -> Optional[str]:
    """Pass a PROVIDER sentence (a third-party response body's own words, e.g. a context-window
    refusal) through the installed redactor before it travels. With no redactor installed
    this is the identity — the status quo for provider text in a plain deployment. Never
    raises; None stays None."""
    if text is None:
        return None
    return _safe_redact(str(text))


def log_crit(where: str, correlation_id: str, exc: BaseException,
             tb: Optional[str] = None) -> None:
    """The single log sink: full exception + traceback under the correlation id the body carries.

    One record, key=value, greppable by ``correlation_id=``. The exception text and the trace
    are redacted by the installed redactor (ASVS V7.1.1) — a driver message that echoes a
    connection string, or an httpx error carrying a full URL, must not become a log leak while
    we are busy closing the response leak.
    """
    if tb is None:
        try:
            tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception:  # noqa: BLE001
            tb = "<traceback unavailable>"
    try:
        exc_text = str(exc)
    except Exception:  # noqa: BLE001
        exc_text = "<unprintable exception>"
    try:
        _log.critical(
            "public_error where=%s correlation_id=%s exc_type=%s exc=%s traceback=%s",
            where, correlation_id, type(exc).__name__,
            _safe_redact(exc_text)[:2000],
            _safe_redact(tb)[:_TRACEBACK_CAP],
        )
    except Exception:  # noqa: BLE001 — a log line must never mask the failure it records
        pass


# ── the two caller-facing shapes ─────────────────────────────────────────────────────

def _record(exc: BaseException, where: str) -> str:
    cid = new_correlation_id()
    log_crit(where, cid, exc)
    return cid


def log_provider_sentence(sentence: Optional[str]) -> None:
    """Server-side WARNING sink for a PROVIDER DIAGNOSTIC sentence. The sentence is already
    credential-redacted by the caller; this passes
    it through the installed log redactor anyway (belt) and writes ONE ``faultline.errors``
    record — the client-facing response carries a curated sentence instead (CWE-209: the
    traceback / provider text stays server-side). Never raises; never logs an unredacted
    sentence (a redactor fault degrades to a shape-only line, fail-closed)."""
    if sentence is None:
        try:
            _log.warning("provider_detail sentence=<unredactable, withheld>")
        except Exception:  # noqa: BLE001 — a log line must never mask the failure it records
            pass
        return
    try:
        _log.warning("provider_detail sentence=%s", _safe_redact(sentence)[:2000])
    except Exception:  # noqa: BLE001 — a log line must never mask the failure it records
        pass


def public_error(exc: BaseException, *, where: str, what: Optional[str] = None) -> dict:
    """The dict shape: ``{"error": <authored>, "correlation_id": <id>}`` for an unexpected
    failure, ``{"error": <refusal sentence>}`` for a refusal.

    ``where`` names the seam (e.g. ``"query.render"``) and is what the log line carries so a
    correlation id can be traced to a code path. ``what`` is an AUTHORED, static phrase the
    route may add ("LLM setup error") — it is prose the route wrote, never derived from ``exc``.
    Merge into an existing payload with ``out.update(public_error(exc, where=…))``.
    """
    if is_refusal(exc):
        return {"error": _refusal_message(exc)}
    cid = _record(exc, where)
    return {"error": (what or GENERIC_ERROR), "correlation_id": cid}


def public_detail(exc: BaseException, *, where: str, what: Optional[str] = None) -> str:
    """The string shape, for ``HTTPException(detail=…)`` / JSON-RPC messages / ``reason`` fields:
    ``"<authored> (ref <id>)"`` for an unexpected failure, the refusal sentence for a refusal."""
    if is_refusal(exc):
        return _refusal_message(exc)
    cid = _record(exc, where)
    return f"{what or GENERIC_ERROR} (ref {cid})"
