"""IS4 — CodeQL py/bad-tag-filter on scripts/validate_wiki.py::strip_html.

The old ``<script.*?</script>`` did not match the end tags browsers accept — ``</script >``,
``</script foo="bar">`` (the HTML parser is forgiving; CodeQL cites
codeql.github.com/codeql-query-help/python/py-bad-tag-filter) — so script bodies leaked into
the "text" the validator scans. The fix matches ``</script\\b[^>]*>`` (and ``<script\\b[^>]*>``
for an open tag with attributes), case-insensitively, same for ``<style>``.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_validate_wiki_tag_filter.py
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_wiki.py"


@pytest.fixture(scope="module")
def vw():
    spec = importlib.util.spec_from_file_location("validate_wiki_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod       # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("raw", [
    "<p>a</p><script>alert(1)</script >b",
    '<p>a</p><script>alert(1)</script foo="bar">b',
    "<p>a</p><SCRIPT type=module>alert(1)</SCRIPT\n>b",
    "<p>a</p><script\tsrc=x>alert(1)</script\t>b",
    "<p>a</p><style>alert(1)</style >b",
    "<p>a</p><style media=all>alert(1)</STYLE foo>b",
])
def test_forgiving_end_tags_are_stripped(vw, raw):
    out = vw.strip_html(raw)
    assert "alert" not in out, out
    assert out == "a b", out


def test_plain_tags_still_stripped_and_text_kept(vw):
    assert vw.strip_html("<h1>Title</h1><p>x &amp; y</p><script>z()</script>") == "Title x & y"


def test_the_pattern_names_the_word_boundary(vw):
    pat = vw._SCRIPT_STYLE_RE.pattern
    assert r"</script\b[^>]*>" in pat and r"</style\b[^>]*>" in pat
