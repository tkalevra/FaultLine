"""#63 pin, writer half: a test module that writes os.environ AT IMPORT (the pattern ~80 modules
use for production flags) gets those values for ITS OWN tests — and nowhere else.

The reader half is tests/test_env_isolation_reader_63.py. pytest imports every module before any
test runs (and every xdist worker imports all of them), so before the tests/conftest.py collection
bracket these import-time writes were visible to the whole session: the reader went red.
"""
import os

# Deliberately module-level, exactly the leaking shape: one set, one delete of a baseline value.
os.environ.setdefault("FL_ENV_ISOLATION_PROBE_63", "writer-module-value")
os.environ.pop("SAAS_COOKIE_SECURE", None)


def test_own_import_time_set_is_visible_to_own_tests():
    assert os.environ.get("FL_ENV_ISOLATION_PROBE_63") == "writer-module-value"


def test_own_import_time_delete_is_visible_to_own_tests():
    assert "SAAS_COOKIE_SECURE" not in os.environ
