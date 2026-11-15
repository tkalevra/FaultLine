"""IS3 — every CodeQL py/polynomial-redos site is linear on 100 kB adversarial input.

15 sites in DEV/review/codeql/3f250759-dirty/results.json, three patterns:

  A. the e-mail shape ``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$`` — SIX definitions (saas/user_console.py,
     mfa.py, tenant_console.py, tenant_signup.py ``_EMAIL_RE``; accounts.py ``_BILLING_EMAIL_RE``;
     admin.py ``_BENCH_EMAIL_RE``) behind TWELVE flagged ``.match`` sites. ``[^@\\s]+`` after the
     ``@`` also matches ``.``, so ``[^@\\s]+\\.[^@\\s]+`` is ambiguous: on ``!@!.!.!.…`` + a
     non-matching tail every split point is retried — quadratic (CodeQL's own message names
     this input). Rewritten to ``^[^@\\s]+@[^@\\s.]+(?:\\.[^@\\s.]+)+$`` — no class overlaps
     ``.``, so there is exactly one way to match: linear.
  B. ``([a-zA-Z]+)\\s*$`` (main.py unit-suffix) and ``([A-Z][a-z]+)\\s+(?:prefers?…)`` with
     re.I (main.py third-party preference) — a greedy letter-run that can START at every
     position of the run; each start re-scans to the end: quadratic on ``AAAA…``. Rewritten
     with a lookbehind ``(?<![a-zA-Z])`` / ``(?<![a-z])`` so a run is entered once, at its
     start. Match results are IDENTICAL (a mid-run start can only succeed where the run-start
     does; parity pinned below).
  C. ``_COORD_SPLIT_RE`` (main.py) — a leading ``\\s*`` on ``re.split``: every start position
     inside a whitespace run re-consumes the run: quadratic on 100 kB of spaces. The pattern is
     kept; the INPUT is bounded before the match: ``re.sub(r"\\s{2,}", " ", sent)`` collapses
     runs to one char so ``\\s*``/``\\s+`` cost O(1) per position (pieces are ``.strip()``ed
     afterwards, so output is unchanged — parity pinned below). This is CodeQL's second
     accepted remedy: "ensure that the strings matched with the regular expression are short
     enough that the time-complexity does not matter"
     (codeql.github.com/codeql-query-help/python/py-polynomial-redos).

BEFORE timings (the revert-red evidence) live in round_1/REGEX_TABLE.md, produced by
round_1/regex_timing.py in per-pattern subprocesses with a wall-clock cap — the old patterns
do not finish 100 kB inside 20 s, so they cannot be timed inline here.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_regex_linear.py
"""

import re
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
N = 100_000
BUDGET_MS = 50.0

LINEAR_EMAIL = r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$"


def _ms(fn, runs: int = 3):
    """Best of ``runs`` — timing noise on a shared box is one-sided (a preempted run only
    reads SLOWER), and the property under test is algorithmic, not a benchmark."""
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - t0) * 1000.0)
    return best


# ── A. the six e-mail definitions (twelve flagged match sites) ────────────────────

EMAIL_SITES = [
    ("saas.user_console", "_EMAIL_RE"),      # user_console.py:506, :1017, :1977, :2133
    ("saas.mfa", "_EMAIL_RE"),               # mfa.py:359
    ("saas.tenant_console", "_EMAIL_RE"),    # tenant_console.py:933, :3905, :5343, :5496
    ("saas.tenant_signup", "_EMAIL_RE"),     # tenant_signup.py:700
    ("saas.accounts", "_BILLING_EMAIL_RE"),  # accounts.py:451
    ("saas.admin", "_BENCH_EMAIL_RE"),       # admin.py:4091
]

EMAIL_ADVERSARIAL = [
    "!@!." + "!." * ((N - 4) // 2) + " ",     # CodeQL's named input + a non-matching tail
    "a@" + "b." * ((N - 2) // 2) + "@",
    "a@" + "." * N,
    "@" * N,
]


@pytest.mark.parametrize("modname,attr", EMAIL_SITES)
def test_email_regex_is_the_linear_form(modname, attr):
    mod = __import__(modname, fromlist=[attr])
    rx = getattr(mod, attr)
    assert rx.pattern == LINEAR_EMAIL, (modname, rx.pattern)


@pytest.mark.parametrize("modname,attr", EMAIL_SITES)
@pytest.mark.parametrize("adv", EMAIL_ADVERSARIAL)
def test_email_regex_linear_on_100kb(modname, attr, adv):
    rx = getattr(__import__(modname, fromlist=[attr]), attr)
    assert len(adv) >= N
    ms = _ms(lambda: rx.match(adv))
    assert ms < BUDGET_MS, f"{modname}.{attr}: {ms:.1f} ms on {len(adv)} chars"


@pytest.mark.parametrize("email,ok", [
    ("a@b.c", True), ("john.doe@example.test", True), ("user+tag@sub.domain.example", True),
    ("a@b_c.d", True), ("x@y", False), ("a@.b", False), ("a@b.", False), ("@b.c", False),
    ("a b@c.d", False), ("a@b..c", False),   # empty DNS label — was accepted by the old form
    ("a@.b.c", False), ("a@b.c.", False),    # leading / trailing empty label (round-2 nit)
])
def test_email_regex_semantics(email, ok):
    for modname, attr in EMAIL_SITES:
        rx = getattr(__import__(modname, fromlist=[attr]), attr)
        assert bool(rx.match(email)) is ok, (modname, email)


# ── B/C. the three src/api/main.py sites (pattern read from SOURCE, not imported) ────

MAIN = (REPO / "src" / "api" / "main.py").read_text(encoding="utf-8")


def _main_line(fragment: str) -> str:
    lines = [ln for ln in MAIN.splitlines() if fragment in ln]
    assert len(lines) == 1, (fragment, len(lines))
    return lines[0]


def test_unit_suffix_regex_linear_and_equivalent():
    line = _main_line("um = re.search(r'(?<![a-zA-Z])([a-zA-Z]++)\\s*+$', s)")
    pat = re.search(r"re\.search\(r'(.*?)', s\)", line).group(1)
    assert "++" in pat and "*+" in pat, "the runs must be possessive (OWASP ReDoS): no give-back"
    rx, old = re.compile(pat), re.compile(r"([a-zA-Z]+)\s*$")
    adv = "A" * N + "!"
    assert _ms(lambda: rx.search(adv)) < BUDGET_MS
    # acceptance parity with the pre-fix pattern, INCLUDING the \n-pivot cases that motivated
    # the possessive form ($ matches before a trailing \n too — a greedy \s* can part there)
    for s in ["5 kg", "5kg ", "abc def", "12", "  ", "k9 lbs", "10 mg/dL", "AAAA!", "x",
              "5 kg\n", "5 kg \n", "kg\n\n", "word\nx", "kg \n \n", "5 kg\t\n"]:
        a, b = old.search(s), rx.search(s)
        assert (a and a.group(1)) == (b and b.group(1)), s


def test_third_party_pref_regex_linear_and_equivalent():
    line = _main_line("r'(?<![a-z])([A-Z][a-z]++)\\s++(?:prefers?|goes\\s++by|known\\s++as|prefer[s]?\\s++to\\s++be\\s++called)\\s++([a-z]++)',")
    pat = re.search(r"r'(.*?)',", line).group(1)
    assert "++" in pat, "the runs must be possessive (OWASP ReDoS): no give-back"
    rx = re.compile(pat, re.IGNORECASE)
    old = re.compile(r'([A-Z][a-z]+)\s+(?:prefers?|goes\s+by|known\s+as|prefer[s]?\s+to\s+be\s+called)\s+([a-z]+)',
                     re.IGNORECASE)
    adv = "a" * N
    assert _ms(lambda: list(rx.finditer(adv))) < BUDGET_MS
    for s in ["Alice prefers bob", "xAlice prefers bob", "Al1ice prefers bob",
              "alice goes by al and Bob prefers robert", "A prefers b", "ALICE KNOWN AS AL",
              "my daughter Sarah prefers to be called Sam", "Jo-Anne prefers jo", "aaaa",
              "Desmond goes by Theo", "Z known as z and Y prefers x", "prefer to be called x",
              "Ann prefers to be called CJ and Bob goes by Bobby"]:
        assert [m.groups() for m in old.finditer(s)] == [m.groups() for m in rx.finditer(s)], s


def _coord_re():
    m = re.search(r'_COORD_SPLIT_RE = re\.compile\(\n\s*r"(.*?)",\n\s*re\.IGNORECASE,', MAIN)
    assert m, "_COORD_SPLIT_RE definition not found"
    return re.compile(m.group(1), re.IGNORECASE)


def _coord_pattern() -> str:
    m = re.search(r'_COORD_SPLIT_RE = re\.compile\(\n\s*r"(.*?)",\n\s*re\.IGNORECASE,', MAIN)
    assert m, "_COORD_SPLIT_RE definition not found"
    return m.group(1)


def test_coord_split_pattern_has_no_backtracking_into_a_whitespace_run():
    """CQ-hardening 2026-09-17: the 2026-09-14 fix bounded the INPUT (the re.sub collapse is
    still there as defense in depth) but CodeQL still flagged the PATTERN — a leading \s* is
    re-consumed at every re.split restart position. The pattern now has NO leading \s* at all
    (the pieces are .strip()ed at the single call site, so the whitespace it ate lands back in
    the left piece) and possessive \s runs elsewhere: no give-back into a run (OWASP ReDoS)."""
    pat = _coord_pattern()
    assert not pat.startswith(r"\s"), "a leading \s* is re-scanned at every split() restart"
    assert r"\s++" in pat and "?+" in pat, pat
    # and the bounding pre-sub is still pinned at the call site
    lines = MAIN.splitlines()
    idx = [i for i, ln in enumerate(lines) if "for sub in _COORD_SPLIT_RE.split(" in ln]
    assert len(idx) == 1, idx
    assert 're.sub(r"\\s{2,}", " ", sent)' in lines[idx[0]], lines[idx[0]]
    assert "codeql[" not in lines[idx[0]] and "codeql[" not in lines[idx[0] - 1], \
        "the redo annotation is gone — the pattern itself is linear now"


def test_coord_split_linear_on_100kb_after_bounding():
    rx = _coord_re()

    def split(sent):
        return rx.split(re.sub(r"\s{2,}", " ", sent))

    for adv in [" " * N, " a" * (N // 2), ", " * (N // 2), "and " * (N // 4), "\t\n" * (N // 2),
                " , " * (N // 3)]:
        ms = _ms(lambda: split(adv))
        assert ms < BUDGET_MS, f"{ms:.1f} ms on {adv[:6]!r}…"


def test_coord_split_parity_after_bounding():
    rx = _coord_re()
    for s in ["I went home and then I slept", "cats, and dogs,  also  birds", "salt   and   pepper",
              "x or y", "as well as z", "noise", "a  ,  and b", "  leading and trailing  "]:
        a = [x.strip() for x in rx.split(s) if x.strip()]
        b = [x.strip() for x in rx.split(re.sub(r"\s{2,}", " ", s)) if x.strip()]
        assert a == b, (s, a, b)


def test_coord_split_parity_with_the_pre_fix_pattern():
    """The no-leading-\s possessive pattern produces the SAME stripped pieces as the pre-fix
    pattern (which ate the leading whitespace into the separator) — the single call site
    strips every piece, so output is unchanged."""
    rx = _coord_re()
    old = re.compile(r"\s*(?:,\s+)?\b(?:and|but|also|plus|then|as\s+well\s+as|along\s+with|or)\b\s+",
                     re.IGNORECASE)
    for s in ["I went home and then I slept", "cats, and dogs,  also  birds", "salt   and   pepper",
              "x or y", "as well as z", "noise", "a  ,  and b", "  leading and trailing  ",
              ", , , x", " and , but  or   as well as z", ",and", " ,and", "a,b,and c",
              "\t\n and x", "a\nand b", "x \n and y", " , \n and z", "leading and",
              "trailing and   ", "  also  "]:
        a = [x.strip() for x in old.split(s) if x.strip()]
        b = [x.strip() for x in rx.split(s) if x.strip()]
        assert a == b, (s, a, b)


def test_coord_split_linear_on_unbounded_whitespace_runs():
    """The 2026-09-14 fix relied on the input being pre-collapsed; the pattern is now linear
    on RAW 100 kB whitespace runs too — no reliance on the caller."""
    rx = _coord_re()
    for adv in [" " * N, " a" * (N // 2), ", " * (N // 2), "and " * (N // 4), "\t\n" * (N // 2),
                " , " * (N // 3), ", " * 100 + " " * N]:
        ms = _ms(lambda: rx.split(adv))
        assert ms < BUDGET_MS, f"{ms:.1f} ms on {adv[:6]!r}…"


def test_url_userinfo_linear_and_equivalent():
    """saas/brain_fault.py::_SECRET_URL_USERINFO (applied at redact.py:205 / brain_fault.py:200).
    The old runs ([^\\s/@]+ on both sides of ':') could each consume the other's separator, so
    every (colon, '@') split was retried — CodeQL's own named input ('!:' repetitions) took
    ~40 s at 100 kB. The new classes exclude their own separators: one parse per position.
    Acceptance AND matched span are unchanged (equivalence argued in the pattern's comment)."""
    bf = (REPO / "saas" / "brain_fault.py").read_text(encoding="utf-8")
    m = re.search(r'_SECRET_URL_USERINFO = re\.compile\(r"(.*?)"\)', bf)
    assert m, "_SECRET_URL_USERINFO definition not found"
    rx, old = re.compile(m.group(1)), re.compile(
        r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@")
    assert "*+" in rx.pattern, rx.pattern
    adv = "a://" + "!:" * (N // 2)
    assert _ms(lambda: rx.sub("[m]@", adv)) < BUDGET_MS
    import random
    rng = random.Random(7)
    alphabet = "ab:/@.+- \t!;"
    cases = ["postgresql://u:p@db.internal:5432/x", "https://admin:Hunter2Password@llm.acme.com/v1",
             "https://x/v1?api-key=k", "a://b:c@d", "a://b:@", "a://:c@", "a://:@",
             "a://b c:d@e", "a://b/c:d@e", "a://b:c/d@e", "ftp://us:er:pa@h/x", "x://u:p@a:h",
             "x://u:p@:a", "x://us@er:pa@h", "://a:b@c", "1a://b:c@d", "a://b:c@d@e:f@g"]
    cases += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(4000)]
    for s in cases:
        a, b = old.search(s), rx.search(s)
        assert bool(a) == bool(b), (repr(s), a and a.group(0), b and b.group(0))
        if a:
            assert a.group(0) == b.group(0), (repr(s), a.group(0), b.group(0))


# ── the OLD forms are shown super-linear at a size that finishes (the revert-red shape) ──

@pytest.mark.parametrize("old,adv,method", [
    (r"^[^@\s]+@[^@\s]+\.[^@\s]+$", lambda n: "!@!." + "!." * ((n - 4) // 2) + " ", "match"),
    (r"([a-zA-Z]+)\s*$", lambda n: "A" * n + "!", "search"),
])
def test_old_forms_grow_superlinearly(old, adv, method):
    """Doubling the input more than doubles the time (≥3×) — the defect, demonstrated, on a
    size that terminates. 100 kB numbers are in round_1/REGEX_TABLE.md (cap-killed)."""
    rx = re.compile(old)
    t1 = _ms(lambda: getattr(rx, method)(adv(4000)))
    t2 = _ms(lambda: getattr(rx, method)(adv(8000)))
    assert t2 > 3 * t1, (t1, t2)
