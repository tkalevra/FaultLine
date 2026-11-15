"""Host-header normalization shared by every allowlist consumer — ONE parser, imported twice.

Lifted VERBATIM out of ``src/mcp/http_server.py`` (the ``_strip_host_port`` /
``_parse_host_list`` pair that classifies the SaaS faces from ``SAAS_ADMIN_HOSTS`` /
``SAAS_TENANT_HOSTS``) so that ``saas/tenant_checkout_routes.py::_request_origin`` can gate a
request-derived Stripe return origin on the SAME allowlist, parsed by the SAME code. It lives in
its own module rather than being imported from ``http_server`` because that module builds the
whole FastAPI app at import time and mounts the ``saas/`` routers — a ``saas`` module importing
``http_server`` would be a circular import through the transport. ``http_server`` keeps its
private names bound to these functions (``_strip_host_port = strip_host_port``) so the conftest
fixture and every existing test keep working unchanged.

Dependency-free on purpose: no FastAPI, no ``os`` reads, no SaaS import. A host string in, a
normalized host out. Reading the env (and WHEN to read it — import-time snapshot in
``http_server``, call-time in the checkout path) is the caller's decision, not this module's.

Grammar: RFC 9110 §7.2 — ``Host = uri-host [ ":" port ]`` — is why a ``:port`` suffix is a
legal, expected part of a Host value and why the allowlist match is done on the PORT-STRIPPED
form (Django's ``ALLOWED_HOSTS`` matches the same way: "case-insensitive, not including port").
"""

from __future__ import annotations


def strip_host_port(host: str) -> str:
    """Lowercase a host, strip a trailing :port (IPv6-literal aware), and normalize the
    FQDN trailing dot(s).

    The trailing-dot strip is SECURITY-load-bearing: a fully-qualified `evil.example.test.`
    (a legal, browser/proxy-acceptable form) must fold to `evil.example.test` or it slips past
    the subdomain base-domain match in `http_server._classify_face` and falls through to the
    ungated "internal" face — re-opening the exact operator-console-exposure landmine commit
    50bc775e closed. Strip BEFORE the port split so `host.:443` normalizes too."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return ""
    if h.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8002
        end = h.find("]")
        if end != -1:
            return h[: end + 1]
        return h
    if ":" in h:
        h = h.rsplit(":", 1)[0]
    return h.rstrip(".")  # re-strip in case a trailing dot preceded the :port


def parse_host_list(raw: str | None) -> frozenset[str]:
    """Comma-separated env value → frozenset of port-stripped, lowercased hosts (empties dropped).

    ``None`` / ``""`` → an EMPTY frozenset. Callers decide what empty means: for the face gate it
    means "every host is internal, gate is a no-op"; for the Stripe return origin it means
    "no request host can be trusted" (fail-closed). Same parser, different policy — by design."""
    return frozenset(
        p for p in (strip_host_port(x) for x in (raw or "").split(",")) if p
    )


# Second-level labels under which registries hand out names to the PUBLIC (co.uk, com.au,
# org.nz, ac.jp, gov.br, edu.au, net.nz, mil.*) — a two-label base whose first label is one of
# these is a public suffix, not a registrable domain, so "everything under it" is everybody.
# Deliberately a SMALL in-code set rather than the Public Suffix List: the closed layer takes no
# new runtime dependency and no network fetch for a rail an operator can set explicitly; the set
# covers the ccTLD second-level scheme in common use, and an operator on any such domain sets
# ``SAAS_TENANT_BASE_DOMAINS`` (which now REPLACES the auto-derive). Growable, never a word zoo.
PUBLIC_SUFFIX_SECOND_LABELS = frozenset({
    "co", "com", "org", "net", "gov", "ac", "edu", "mil", "ltd", "plc", "nhs", "sch", "gen",
})


def auto_derived_base(host: str) -> str:
    """The registrable-base guess for a configured tenant host — its last two labels — or ``""``
    when that guess would be a PUBLIC SUFFIX (``app.example.co.uk`` → ``co.uk`` → refused) or the
    host has fewer than two labels. A refused auto-derive yields NO base for that host: the
    explicit hosts still serve, nothing under ``co.uk`` does (fail-closed)."""
    parts = (host or "").split(".")
    if len(parts) < 2 or not all(parts[-2:]):
        return ""
    if parts[-2] in PUBLIC_SUFFIX_SECOND_LABELS:
        return ""
    return ".".join(parts[-2:])


def derive_tenant_base_domains(tenant_hosts, raw: str | None) -> frozenset[str]:
    """Registrable base domain(s) whose ``*.subdomains`` are the per-tenant scoped surface.

    LIFTED from ``http_server._derive_tenant_base_domains`` (checkout-origin-authority round 3)
    so the Stripe return-origin check honours the same rule the face gate honours.

    SEMANTICS (round 4 — corrected): an explicit ``SAAS_TENANT_BASE_DOMAINS`` (comma-separated)
    REPLACES the auto-derive. It used to be UNIONed with it, which made the documented remedy
    for a public-suffix host INERT — setting the env could never REMOVE the naive ``co.uk`` base
    the auto-derive kept adding. When the env is unset, each tenant host contributes its last two
    labels UNLESS that pair is a public-suffix shape (``auto_derived_base``), so
    ``SAAS_TENANT_HOSTS=app.example.co.uk`` now derives NOTHING rather than ``co.uk``."""
    explicit = parse_host_list(raw)
    if explicit:
        return frozenset(explicit)
    bases = set()
    for h in tenant_hosts or ():
        b = auto_derived_base(h)
        if b:
            bases.add(b)
    return frozenset(bases)


def host_is_served_face(host: str, hosts, bases) -> bool:
    """Is a CANONICAL host (already through ``strip_host_port``) one this deployment serves?

    True iff it is in ``hosts`` (the explicit ``SAAS_TENANT_HOSTS ∪ SAAS_ADMIN_HOSTS``) or it is a
    base in ``bases`` or a subdomain of one — the same suffix rule ``http_server._classify_face``
    applies to route ``*.<tenant-base>`` to the tenant face. A host the face gate would serve
    ``/dashboard/subscribe.html`` on is a host Stripe may send the customer back to; a host it
    would not serve is not. Empty ``hosts`` AND empty ``bases`` → False for every host."""
    if not host:
        return False
    if host in (hosts or ()):
        return True
    for base in bases or ():
        if host == base or host.endswith("." + base):
            return True
    return False


PORT_MIN, PORT_MAX = 1, 65535


def canonical_port(port: str | None) -> str:
    """``"08002"`` → ``"8002"``; ``None``/empty → ``""``. Callers echo THIS, never the raw digits,
    so an origin is never ``host:08002`` (RFC 9110 §7.2 port is a decimal number, no padding)."""
    if port is None or port == "":
        return ""
    try:
        return str(int(port))
    except (TypeError, ValueError):
        return ""


def port_in_range(port: str | None) -> bool:
    """``None``/empty (no port given) → True; otherwise the decimal port must be 1..65535
    (RFC 9110 §7.2 ``port`` is a decimal; 0 and >65535 are not usable TCP ports)."""
    if port is None or port == "":
        return True
    try:
        return PORT_MIN <= int(port) <= PORT_MAX
    except (TypeError, ValueError):
        return False
