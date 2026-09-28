"""INSTALL LANGUAGE — the one runtime signal for which language this install parses.

[it branch] ``FAULTLINE_LANGUAGE`` is written into ``.env`` by quickstart.py's language gate
(``_LANGUAGES`` table: ``it`` → ``SPACY_MODEL=it_core_news_sm`` + ICU ``it-IT``) and passed
through docker-compose. EMPTY or ``en`` means English — which is also what every test run and
every pre-language-gate install sees, so an English install is byte-for-byte unchanged.

WHY THIS EXISTS. The engine carried into this branch from English ``main`` contains behaviour
derived from ENGLISH grammar that has no Italian equivalent yet: English closed-class words in
surface regexes, English-only lexical resources (Princeton WordNet), and gates whose inverse
branch would fire on every Italian sentence ("no English wh-word ⇒ not a question"). An inert
English matcher is harmless on Italian text; an English gate whose NEGATIVE branch acts is not.
Those call sites ask :func:`english_grammar_available` and stay inert when it is False, rather
than shipping a wrong heuristic (doctrine: gate it and document it).

Pure: stdlib only, read on every call (cheap), so tests can flip the env with monkeypatch.
"""

from __future__ import annotations

import os

_ENGLISH = ("", "en")


def install_language() -> str:
    """The install language code, lowercased (``""`` when unset → English)."""
    return (os.environ.get("FAULTLINE_LANGUAGE") or "").strip().lower()


def english_grammar_available() -> bool:
    """True when the install parses ENGLISH (``FAULTLINE_LANGUAGE`` unset/empty/``en``).

    False on the Italian install: an English-grammar-derived heuristic must stay inert there."""
    return install_language() in _ENGLISH
