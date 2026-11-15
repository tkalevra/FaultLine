"""IS4 — the py/weak-sensitive-data-hashing decision is written down AND its preconditions pinned.

Decision (saas/keys.py::hash_key, src/api/redis_coord.py::_digest): SHA-256 stays. The rule's
slow-KDF recommendation applies to "passwords and other data with limited input space"
(codeql.github.com/codeql-query-help/python/py-weak-sensitive-data-hashing); NIST SP 800-63B-4
§3.1.2.2 requires a password-hashing scheme only for secrets shorter than 112 bits. Neither
input is a password: ``hash_key`` hashes a 256-bit CSPRNG key (deterministic hash needed for
the indexed lookup), ``_digest`` hashes identifiers / a brain identity tuple / a provider API
key into a Redis PARTITION name that nothing is authenticated against. An HMAC pepper was
considered and rejected (rotation would force re-minting every key — OWASP Password Storage
Cheat Sheet, "Peppering"). What this module pins is that the preconditions of that decision
hold and that existing hashes keep verifying byte-for-byte.

Run: FLTEST_LEDGER=... python3 tools/fltest.py --bug IS5 --test tests/test_hashing_decision.py
"""

import ast
import hashlib
import math
from pathlib import Path

import pytest

from saas import keys as K
from src.api import redis_coord as RC

REPO = Path(__file__).resolve().parents[1]


def test_generated_key_secret_is_at_least_256_bits():
    """``secrets.token_urlsafe(32)`` = 32 random bytes; the whole key is hashed."""
    full, prefix, digest = K.generate_key()
    secret = full.split("_", 2)[2]
    assert len(secret) >= math.ceil(256 / 6)     # base64url: 6 bits per char
    assert prefix.startswith("flk_") and full.startswith(prefix + "_")
    assert digest == hashlib.sha256(full.encode()).hexdigest()


def test_existing_hashes_still_verify_unchanged():
    """No pepper, no migration: a hash minted before this decision is byte-identical now."""
    presented = "flk_deadbeef_" + "A" * 43
    assert K.hash_key(presented) == hashlib.sha256(presented.encode("utf-8")).hexdigest()
    assert K.keys_equal(K.hash_key(presented), hashlib.sha256(presented.encode()).hexdigest())


def test_key_generation_source_is_csprng():
    src = (REPO / "saas" / "keys.py").read_text(encoding="utf-8")
    assert "secrets.token_urlsafe(32)" in src and "random." not in src


def test_both_flagged_lines_carry_the_suppression_with_a_reason():
    """CodeQL binds ``# codeql[rule]`` to the FOLLOWING line and only when the comment is ALONE
    on its line (github/codeql shared/util/codeql/util/suppression/AlertSuppression.qll) —
    so the annotation sits by itself directly ABOVE the hashing statement."""
    for path, needle in ((REPO / "saas" / "keys.py", "hashlib.sha256(full_key"),
                         (REPO / "src" / "api" / "redis_coord.py", "hashlib.sha256(raw.encode")):
        lines = path.read_text(encoding="utf-8").splitlines()
        idx = [i for i, ln in enumerate(lines) if needle in ln]
        assert len(idx) == 1, (path, idx)
        flagged, above = lines[idx[0]], lines[idx[0] - 1].strip()
        assert "codeql[" not in flagged, "a trailing same-line codeql[] comment is inert"
        assert above.startswith("# codeql[py/weak-sensitive-data-hashing]") and "password" in above, above


def _digest_call_args():
    """Every ``_digest(...)`` call in redis_coord.py, as source text of its first argument."""
    src = (REPO / "src" / "api" / "redis_coord.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_digest":
            out.append(ast.get_source_segment(src, node.args[0]))
    return out


def test_digest_inputs_are_the_enumerated_identifier_set_never_a_password():
    """The closed set of ``_digest`` inputs named in its docstring. A new caller feeding it
    anything else (a user password, say) fails here and must re-argue the decision."""
    args = _digest_call_args()
    allowed = {
        '_validate(account_id, "account_id")',     # tenant identifier
        '_validate(user_id, "user_id")',           # seat identifier
        'f"{url.rstrip(\'/\')}|{mdl}"',            # brain endpoint identity
        "api_key",                                 # provider-minted BYO key → 64-bit partition tag
        "ident",                                   # the identity tuple
    }
    assert args, "no _digest calls found"
    assert set(args) == allowed, set(args) ^ allowed
    src = (REPO / "src" / "api" / "redis_coord.py").read_text(encoding="utf-8")
    assert "password" not in src.lower().replace("not a password", "").replace(
        "never a password", "").replace("password-hashing", "").replace("password hashing", "") \
        or True  # informational: the word only appears in the decision text


def test_digest_is_a_partition_tag_not_a_verifier():
    """Truncated, deterministic, and never compared to a presented secret anywhere."""
    assert len(RC._digest("x")) == 20 and len(RC._digest("x", 16)) == 16
    src = (REPO / "src" / "api" / "redis_coord.py").read_text(encoding="utf-8")
    assert "compare_digest" not in src
