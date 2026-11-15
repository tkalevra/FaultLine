"""A Class-C `context` blob must ALWAYS render in the HELD band — never asserted.

WHY. The recall emitter used to assert a `context` blob whenever its confidence cleared
_RECALL_ASSERT_CONF_FLOOR (0.35), justified in-code as "These are the user's OWN words,
captured verbatim... credible-but-unstructured user content -> ASSERT it, not hedged."

That premise is false in practice, and we cannot tell the difference:
  * store_context records NO authorship. Live 500-point sample of the Class-C collection:
    `source` is only 'mcp' or null, `context_type` is always 'unstructured'. Nothing
    distinguishes a user sentence from an assistant turn.
  * A 20,793-blob classification found 16-44% third-person/assistant-authored prose, rising to
    72-82% on document-mode seats — markdown answers, "5. **Notify your healthcare
    provider**...", bare "assistant:", even "__mcp_reachability_probe__".
  * store_context stamps confidence 0.4, clearing the 0.35 floor — so on an UNSCOPED query the
    engine asserted LLM-generated prose as the user's own grounded memory.

Putting words in the user's mouth is the worst failure this system can have, and it is exactly
what CLAUDE.md already forbids: "store_context (rel_type=='context') prose -> ALWAYS held/soft".
The code had drifted from its own spec.

SCOPE OF THIS TEST — stated honestly: the context branch lives inside `recall_memory_tool`, an
async function that performs network I/O, so this asserts the CONTRACT STRUCTURALLY rather than
by executing the emitter. It will catch a regression that re-introduces conditional asserting,
which is the failure mode that actually occurred. A full behavioural test needs a mocked
transport and belongs with the recall-render suite.
"""
import pathlib
import re

SERVER = pathlib.Path(__file__).resolve().parents[1] / "src" / "mcp" / "server.py"


def _context_branch() -> str:
    """The `rel_type == 'context'` branch of the recall emitter.

    Extract by INDENTATION, not by the first `continue` — the branch contains an early
    `if not raw_text: continue` guard, so cutting at the first `continue` truncates before the
    banding decision and makes the assertions below vacuous. (It did exactly that on the first
    write of this file, which is the kind of silently-passing test this suite exists to avoid.)
    """
    lines = SERVER.read_text().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if 'if fact.get("rel_type") == "context":' in l)
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for l in lines[start + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break                      # dedented back out of the branch
        out.append(l)
    return "\n".join(out)


def test_context_blob_is_appended_to_hold_lines():
    branch = _context_branch()
    assert "hold_lines.append(text)" in branch, \
        "a context blob must land in the HELD band"


def test_context_blob_is_never_conditionally_asserted():
    """The regression guard: no branch may route a context blob into assert_lines."""
    branch = _context_branch()
    assert "assert_lines" not in branch, (
        "a `context` blob must NEVER reach assert_lines — authorship is not recorded, so it "
        "may be assistant-generated prose, and asserting it invents a user memory")


def test_context_banding_does_not_depend_on_a_confidence_threshold():
    """It previously held only because 0.30 < 0.35 — two independently-tunable env vars in two
    services, with nothing pinning the coupling. One edit re-opened the firewall silently."""
    branch = _context_branch()
    assert "_RECALL_ASSERT_CONF_FLOOR" not in branch, (
        "context banding must not be coupled to a numeric confidence floor — make held-ness "
        "unconditional, so a tuning change cannot silently turn blobs back into assertions")


def test_the_documented_contract_is_restored():
    """CLAUDE.md is the spec: context prose is ALWAYS held/soft. Keep code and doc in agreement."""
    claude_md = (pathlib.Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text()
    m = re.search(r"store_context.*?rel_type==\"context\".*?prose.*?held", claude_md, re.S | re.I)
    assert m, "CLAUDE.md should still document context prose as always held/soft"
