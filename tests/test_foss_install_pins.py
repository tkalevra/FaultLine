"""FOSS fresh-install pins (#142): a new upstream major must not break a fresh image build.

`gliner2` was unpinned; PyPI served 2.0.0, which moved torch/transformers into a `[local]`
extra, so the Dockerfile's GLiNER2 pre-download died with ModuleNotFoundError: torch on every
fresh clone. These pins fail if any runtime dependency loses its upper bound again.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]

# Extras that ship into the image (the Dockerfile installs `.[api]`) or into the MCP server.
_SHIPPED_EXTRAS = ("api", "mcp-http")


def _name(req: str) -> str:
    return re.split(r"[\s\[<>=!~;]", req, maxsplit=1)[0].lower()


def _bounded(req: str) -> bool:
    spec = req.split(";", 1)[0]
    return "==" in spec or "<" in spec


def _shipped_requirements() -> list[str]:
    reqs = list(_PROJECT["dependencies"])
    for extra in _SHIPPED_EXTRAS:
        reqs += _PROJECT["optional-dependencies"][extra]
    return reqs


def test_gliner2_pinned_below_major_2():
    gl = [r for r in _PROJECT["dependencies"] if _name(r) == "gliner2"]
    assert gl, "gliner2 must be a declared runtime dependency"
    spec = gl[0]
    m = re.search(r"==\s*(\d+)\.", spec)
    assert m and int(m.group(1)) < 2, f"gliner2 must be pinned to a validated 1.x release, got {spec!r}"


def test_every_shipped_dependency_has_an_upper_bound():
    loose = [r for r in _shipped_requirements() if not _bounded(r)]
    assert not loose, f"unbounded runtime dependencies (a new upstream major breaks fresh installs): {loose}"


def test_transformers_bounded_to_4x():
    tr = [r for r in _PROJECT["dependencies"] if _name(r) == "transformers"]
    assert tr and "<5" in tr[0].replace(" ", ""), "transformers must be bounded to the validated 4.x line"
