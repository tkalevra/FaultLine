"""THE LANDING SITE'S INTERNAL LINKS AND ANCHORS MUST GO SOMEWHERE — pinned as OUTCOMES.

WHY THIS FILE EXISTS. A critic pass found eight dead cross-page anchors on the public site
(``index.html#product`` / ``#how`` / ``#pricing`` / ``#teams`` from research.html and geek.html,
``#pricing`` self-referenced on quick.html) — every one of them pointing into
``saas/landing/index.html``, which is a SPLASH CHOOSER and has never carried a single one of
those sections. Nothing caught them because nothing looked: the pages are static files with no
build step, so a renamed section or a copy-pasted footer breaks silently and stays broken.

It also found the FUNNEL pointing at the wrong door: the Team and Enterprise "Start a team" /
"Get started" buttons on numbers.html and tour.html linked to
``https://app.faultline.ca/dashboard/user.html`` — the INDIVIDUAL console, which cannot sell a
Team plan — while business.html's equivalent buttons correctly went to ``signup-team.html``.

THREE THINGS ARE PINNED, and each is an OUTCOME a reader of the page would experience:

  1. EVERY internal link resolves — the file exists, and if the link carries a ``#fragment``,
     an element with that id (or an ``<a name>``) exists ON THAT PAGE.
  2. The paid-tier CTAs land on a door that can actually sell the tier.
  3. The pages carry NO inline script and NO inline event handler, because the served CSP is
     ``script-src 'self'`` with no ``'unsafe-inline'`` — an inline-script build renders BLANK.

METHOD NOTE, LOAD-BEARING: all three are asserted with a REAL HTML PARSER
(``html.parser.HTMLParser``, stdlib), never a regex. A CSS comment or a prose paragraph that
merely MENTIONS ``<script>`` is invisible to a parser and a false positive to a grep; conversely
a regex for ``href="..."`` misses an attribute split across lines or written with single quotes.
The parser sees exactly what a browser's tokenizer sees, which is the only thing that matters.

NOT ASSERTED HERE, deliberately: external ``http(s)://`` links (no network in a unit test — a
flaky test that fails on someone else's outage teaches people to ignore it) and ``mailto:``
targets (a mailbox's existence is not knowable from the repo).
"""

from __future__ import annotations

import os
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
LANDING = os.path.join(_REPO, "saas", "landing")

# Root-absolute paths that are served by the APPLICATION, not by a file in saas/landing/.
# ``/.well-known/security.txt`` is the RFC 9116 well-known location ("The file MUST be placed
# under the /.well-known/ path … of a domain name or IP address", RFC 9116 §3) and is generated
# by ``src/mcp/http_server.py::_security_txt``; ``/security.txt`` is the legacy top-level twin
# the same module also serves. Neither can be a file on disk, and both are live routes.
APP_SERVED_ABSOLUTE_PATHS = {"/.well-known/security.txt", "/security.txt"}

# Schemes a static-file check cannot and should not resolve.
_SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")

# Attributes that carry a URL we care about.
_URL_ATTRS = ("href", "src")


# The JavaScript MIME type essences, verbatim from the HTML Standard's "JavaScript MIME type"
# definition (§ MIME types / "javaScript MIME type essence match"). A <script> carrying one of
# these (or no type at all, or type="module") is EXECUTABLE and therefore CSP-relevant; anything
# else is a data block.
_JS_MIME_ESSENCES = frozenset({
    "application/ecmascript", "application/javascript", "application/x-ecmascript",
    "application/x-javascript", "text/ecmascript", "text/javascript", "text/javascript1.0",
    "text/javascript1.1", "text/javascript1.2", "text/javascript1.3", "text/javascript1.4",
    "text/javascript1.5", "text/jscript", "text/livescript", "text/x-ecmascript",
    "text/x-javascript",
})


def _is_executable_script_type(type_attr) -> bool:
    """True when a <script type=…> would actually be prepared and RUN (and so is CSP-relevant)."""
    if type_attr is None:
        return True                      # no type = classic script
    t = type_attr.strip().lower()
    if t == "":
        return True                      # empty type = classic script (HTML Standard)
    if t == "module":
        return True
    return t.split(";", 1)[0].strip() in _JS_MIME_ESSENCES


class _PageParser(HTMLParser):
    """Collect ids, fragment targets, links, inline scripts and inline handlers from one page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.links: list[tuple[str, str, int]] = []      # (attr, value, line)
        self.inline_scripts: list[int] = []              # line numbers
        self.inline_handlers: list[tuple[str, str, int]] = []   # (tag, attr, line)
        self.javascript_urls: list[tuple[str, int]] = []
        self._in_script_without_src = False
        self._script_line = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        d = dict(attrs)
        line = self.getpos()[0]
        if d.get("id"):
            self.ids.add(d["id"])
        # <a name="x"> is still a valid fragment target in HTML5 parsers for legacy documents.
        if tag == "a" and d.get("name"):
            self.ids.add(d["name"])
        for attr, value in d.items():
            if attr.startswith("on") and value is not None:
                self.inline_handlers.append((tag, attr, line))
        for k in _URL_ATTRS:
            v = d.get(k)
            if not v:
                continue
            if v.strip().lower().startswith("javascript:"):
                self.javascript_urls.append((v, line))
                continue
            self.links.append((k, v, line))
        if tag == "script":
            # An external script (src=…) is fine under script-src 'self'; an INLINE one is not
            # — UNLESS it is a DATA BLOCK. Per the HTML Standard (§4.12.1 "The script element",
            # "Classic scripts and JavaScript module scripts can be embedded inline… When used to
            # include data blocks, the data must be embedded inline, the format of the data must
            # be given using the type attribute … and the contents of the script element must
            # conform to the requirements defined for the format used"), a <script> whose type is
            # neither a JavaScript MIME type nor "module" is NEVER EXECUTED. CSP hooks only into
            # the script-preparation path for classic/module scripts (CSP Level 3 §"Should
            # element's inline behavior be blocked…"), so `application/ld+json` — which is what
            # every inline block on this site is (schema.org markup for search engines) — is not
            # a script-src subject and must not be reported. Flagging it would train people to
            # ignore this test.
            self._in_script_without_src = ("src" not in d) and _is_executable_script_type(d.get("type"))
            self._script_line = line

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script_without_src = False

    def handle_data(self, data: str) -> None:
        if self._in_script_without_src and data.strip():
            self.inline_scripts.append(self._script_line)


def _html_files() -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(LANDING):
        for f in files:
            if f.endswith(".html"):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def _parse(path: str) -> _PageParser:
    p = _PageParser()
    with open(path, encoding="utf-8") as fh:
        p.feed(fh.read())
    return p


@pytest.fixture(scope="module")
def parsed() -> dict[str, _PageParser]:
    files = _html_files()
    assert files, (
        f"no .html files found under {LANDING} — this suite would pass vacuously, which is "
        f"worse than failing. Check the path before 'fixing' the assertion."
    )
    return {f: _parse(f) for f in files}


def _rel(path: str) -> str:
    return os.path.relpath(path, _REPO)


# ════════════════════════════════════════════════════════════════════════════════════
# 1 — EVERY INTERNAL LINK RESOLVES, AND EVERY FRAGMENT HAS A TARGET
# ════════════════════════════════════════════════════════════════════════════════════

def test_every_internal_link_and_anchor_on_the_landing_site_resolves(parsed):
    """A dead internal link or a fragment with no matching id is a broken promise on a page a
    stranger reads before deciding to pay us. Eight of them shipped; this is the net."""
    broken: list[str] = []
    for path, page in parsed.items():
        base = os.path.dirname(path)
        for _attr, href, line in page.links:
            raw = href.strip()
            if raw.lower().startswith(_SKIP_SCHEMES):
                continue
            split = urlsplit(raw)
            if split.scheme or split.netloc:
                continue
            target_path = unquote(split.path)
            fragment = unquote(split.fragment)

            if target_path.startswith("/"):
                if target_path in APP_SERVED_ABSOLUTE_PATHS:
                    continue
                candidate = os.path.join(LANDING, target_path.lstrip("/"))
                if not os.path.exists(candidate):
                    broken.append(
                        f"{_rel(path)}:{line}  {raw}  → no file at {_rel(candidate)} and it is "
                        f"not one of the application-served paths "
                        f"{sorted(APP_SERVED_ABSOLUTE_PATHS)}")
                continue

            if target_path:
                candidate = os.path.normpath(os.path.join(base, target_path))
                if not os.path.exists(candidate):
                    broken.append(f"{_rel(path)}:{line}  {raw}  → MISSING FILE {_rel(candidate)}")
                    continue
            else:
                candidate = path   # same-page fragment

            if fragment and candidate.endswith(".html"):
                target_page = parsed.get(candidate)
                if target_page is None:
                    target_page = _parse(candidate)
                if fragment not in target_page.ids:
                    broken.append(
                        f"{_rel(path)}:{line}  {raw}  → DEAD ANCHOR: {_rel(candidate)} has no "
                        f"element with id={fragment!r}")

    assert not broken, (
        "broken internal link(s) / anchor(s) on the public landing site:\n  "
        + "\n  ".join(broken)
        + "\n\nEither add the missing section id to the target page, or repoint the link at the "
          "page that genuinely owns that content. index.html is a SPLASH CHOOSER — it carries no "
          "#product / #how / #pricing / #teams sections and is not the place to add them."
    )


# ════════════════════════════════════════════════════════════════════════════════════
# 2 — A PAID-TIER CTA MUST LAND ON A DOOR THAT CAN SELL THAT TIER
# ════════════════════════════════════════════════════════════════════════════════════

# The individual "My Memory" console. It provisions a SEAT on the shared house account; it has no
# Team/Enterprise checkout, no company field and no workspace-owner concept. A Team buyer landing
# here has to work out for themselves that they are in the wrong product.
_INDIVIDUAL_CONSOLE = "dashboard/user.html"

# The self-serve TENANT door (saas/tenant_signup.py, POST /signup/team). It takes a company name,
# creates a workspace with the buyer as owner, and — per its own module docstring — routes an
# Enterprise-sized buyer onward rather than auto-granting the $1500 base package to a stranger.
_TEAM_DOOR = "signup-team.html"

# The buttons that START a paid tenant purchase, by their visible label.
_TENANT_CTA_LABELS = ("start a team", "get started", "start free")


def test_no_tenant_cta_sends_a_team_buyer_to_the_individual_console(parsed):
    """``numbers.html`` and ``tour.html`` both pointed their Team AND Enterprise buttons at the
    individual console while ``business.html`` pointed its identical buttons at the team door.
    A purchase CTA that lands somewhere the tier cannot be bought is a lost sale that looks like
    a working page."""
    offenders: list[str] = []
    for path in sorted(parsed):
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        for label in _TENANT_CTA_LABELS:
            # Find each anchor whose visible text is one of the purchase labels, then read the
            # href off THAT anchor. Parsed rather than regexed: we need the tag boundaries.
            for href, text in _anchor_pairs(html):
                if label != text.strip().lower().rstrip(" ▸›→").strip():
                    continue
                if _INDIVIDUAL_CONSOLE in href:
                    offenders.append(
                        f"{_rel(path)}  <a href={href!r}>{text.strip()!r}</a> — a paid-tenant "
                        f"CTA pointing at the INDIVIDUAL console")
    assert not offenders, (
        "paid-tier call(s)-to-action landing on the individual console:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + f"\n\nSend them to {_TEAM_DOOR} (the self-serve tenant door), the way business.html "
          f"already does."
    )


class _AnchorText(HTMLParser):
    """Pair every <a> with its flattened visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: list[tuple[str, str]] = []
        self._stack: list[list] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._stack.append([dict(attrs).get("href", ""), []])

    def handle_data(self, data):
        if self._stack:
            self._stack[-1][1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, chunks = self._stack.pop()
            self.pairs.append((href, "".join(chunks)))


def _anchor_pairs(html: str) -> list[tuple[str, str]]:
    p = _AnchorText()
    p.feed(html)
    return p.pairs


# ════════════════════════════════════════════════════════════════════════════════════
# 3 — CSP: script-src 'self' MEANS NO INLINE SCRIPT AND NO on*= HANDLER
# ════════════════════════════════════════════════════════════════════════════════════

def test_landing_pages_carry_no_inline_script_or_handler(parsed):
    """The served CSP is ``script-src 'self'`` with no ``'unsafe-inline'``. An inline ``<script>``
    or an ``on*=`` attribute is not degraded — it is BLOCKED, and the page renders inert. Checked
    with the parser: a ``<script>`` written inside a CSS comment or quoted in prose is not a
    script tag, and a grep cannot tell the difference."""
    problems: list[str] = []
    for path, page in parsed.items():
        for line in page.inline_scripts:
            problems.append(f"{_rel(path)}:{line}  inline <script> with no src=")
        for tag, attr, line in page.inline_handlers:
            problems.append(f"{_rel(path)}:{line}  <{tag} {attr}=…> inline event handler")
        for value, line in page.javascript_urls:
            problems.append(f"{_rel(path)}:{line}  javascript: URL — {value[:60]!r}")
    assert not problems, (
        "CSP violation(s) — the landing CSP is script-src 'self' with NO 'unsafe-inline':\n  "
        + "\n  ".join(problems)
        + "\n\nMove the code into an external .js file in saas/landing/ and load it with "
          "<script src=…>."
    )
