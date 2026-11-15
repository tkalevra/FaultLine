"""A comp must expire on the same day for every process that asks.

THE BUG. `comp_state` decided expiry in Python: `until < date.today()`. `date.today()` is the
LOCAL date of whichever container evaluated it, compared against a DB DATE an operator wrote.
Two consequences, both money-affecting:

  * this deployment runs America/Toronto (UTC-5/-4), so between local midnight and UTC midnight a
    comp that lapsed YESTERDAY still read `active` — and `active` is what `_apply_comp` uses to
    zero the payable in `invoicing.build_invoice`, so the customer was not billed for those hours;
  * two processes in different zones could disagree about the same account at the same instant.

THE FIX. Postgres decides, in UTC, in the same query that reads the row. UTC rather than the
session TimeZone so every process agrees, and because it fails in the direction this function
already documents for itself — "a control-plane read failure must never silently zero a bill".
For a deployment behind UTC the UTC date rolls over first, so an expiring comp stops zeroing the
bill sooner rather than later.
"""
import inspect
import re

from saas import entitlements


def _src():
    return inspect.getsource(entitlements.comp_state)


def test_expiry_is_not_computed_from_the_process_clock():
    """The regression guard. Any local-clock comparison here reintroduces the bug."""
    body = re.sub(r"#.*$", "", _src(), flags=re.M)          # strip comments: the fix DESCRIBES
    body = re.sub(r'""".*?"""', "", body, flags=re.S)        # the old code, and so does the docstring
    for banned in ("date.today()", "datetime.now()", "datetime.today()"):
        assert banned not in body, (
            f"comp_state decides expiry with {banned} — a comp would expire at a different "
            f"moment in every timezone, and `active` zeroes the invoice payable")


def test_expiry_is_decided_by_the_database_in_utc():
    """Positive half: asserting the absence of the old call is not the same as asserting the
    presence of the right one."""
    body = _src()
    assert "AT TIME ZONE 'utc'" in body, "expiry is not pinned to UTC"
    assert "comp_until <" in body, "the DB is not computing the expiry comparison"


def test_the_row_unpack_matches_the_select_list():
    """The fix widened the SELECT by one column. If a future edit adds or drops a column without
    touching the unpack, comp_state raises ValueError on EVERY call — and its own except arm does
    not catch that, because the unpack happens after the try block. That would fail closed to a
    500 rather than silently, but it is worth pinning."""
    body = _src()
    select = re.search(r'"SELECT (.*?)FROM public\.accounts', body, re.S)
    assert select, "could not find the SELECT"
    cols = select.group(1)
    # count top-level commas outside parentheses = column count - 1
    depth = 0
    n = 1
    for ch in cols:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    unpack = re.search(r"^\s*(comped[^=]*)=\s*row\s*$", body, re.M)
    assert unpack, "could not find the row unpack"
    names = [x.strip() for x in unpack.group(1).split(",") if x.strip()]
    assert len(names) == n, (
        f"SELECT returns {n} columns but the unpack takes {len(names)}: {names}")


def test_active_still_requires_the_flag_as_well_as_the_date():
    """An un-comped account must never read active because its comp_until happens to be future —
    the two conditions are AND, and a bug here would zero bills for accounts that never had a
    comp at all."""
    body = _src()
    assert re.search(r'"active":\s*bool\(comped\)\s*and\s*not\s*expired', body), (
        "active is no longer (comped AND not expired)")
