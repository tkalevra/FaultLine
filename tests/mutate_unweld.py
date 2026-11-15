"""Mutation runner: re-introduce each original defect, prove the suite goes RED."""
import io
import shutil
import subprocess
import sys

PATH = "src/api/main.py"
BACKUP = PATH + ".mutbak"
ENV_DSN = "postgresql://test:test@127.0.0.1:55433/test"

MUTATIONS = [
    (
        "M1 restore the is_preferred=true predicate (the original defect 3)",
        '"DELETE FROM entity_aliases WHERE entity_id = %s AND alias = %s",',
        '"DELETE FROM entity_aliases WHERE entity_id = %s AND alias = %s AND is_preferred = true",',
    ),
    (
        "M2 drop .lower() on the alias key (original defect 2)",
        'key = (alias or "").strip().lower()',
        'key = (alias or "").strip()',
    ),
    (
        "M3 drop .strip() on the alias key",
        'key = (alias or "").strip().lower()',
        'key = (alias or "").lower()',
    ),
    (
        "M4 never promote a replacement display name",
        "    cur.execute(\n        f\"\"\"\n        UPDATE entity_aliases SET is_preferred = true",
        "    return (removed, 0)\n    cur.execute(\n        f\"\"\"\n        UPDATE entity_aliases SET is_preferred = true",
    ),
    (
        "M5 promote by age only, ignoring the trust ladder",
        "ORDER BY {_ALIAS_RANK_SQL} DESC, created_at ASC",
        "ORDER BY created_at ASC",
    ),
    (
        "M6 drop the NOT EXISTS guard (promote even when one is already preferred)",
        "          AND NOT EXISTS (\n                SELECT 1 FROM entity_aliases\n                WHERE entity_id = %s AND is_preferred = true\n            )",
        "          AND (%s IS NOT NULL)",
    ),
    (
        "M7 helper commits, stealing the caller's transaction",
        "    removed = cur.rowcount or 0\n    if not removed:",
        "    removed = cur.rowcount or 0\n    cur.connection.commit()\n    if not removed:",
    ),
    (
        "M8 ignore the entity scope, delete the alias everywhere",
        '"DELETE FROM entity_aliases WHERE entity_id = %s AND alias = %s",\n        (entity_id, key),',
        '"DELETE FROM entity_aliases WHERE %s IS NOT NULL AND alias = %s",\n        (entity_id, key),',
    ),
]


def run():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_retract_unwelds_alias.py", "-q",
         "--no-header", "-x", "-p", "no:randomly"],
        capture_output=True, text=True, timeout=900,
        env={**__import__("os").environ, "UNWELD_TEST_DSN": ENV_DSN},
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    shutil.copy(PATH, BACKUP)
    src = io.open(PATH, encoding="utf-8").read()
    results = []
    try:
        for name, old, new in MUTATIONS:
            if src.count(old) != 1:
                results.append((name, "ANCHOR-MISS", f"count={src.count(old)}"))
                continue
            io.open(PATH, "w", encoding="utf-8").write(src.replace(old, new, 1))
            code, out = run()
            tail = [l for l in out.splitlines() if "passed" in l or "failed" in l or "error" in l.lower()]
            verdict = "RED (good)" if code != 0 else "GREEN (NO TEETH)"
            results.append((name, verdict, tail[-1].strip() if tail else out.strip()[-160:]))
            io.open(PATH, "w", encoding="utf-8").write(src)
    finally:
        shutil.copy(BACKUP, PATH)

    print("\n=== MUTATION REPORT ===")
    for name, verdict, detail in results:
        print(f"{verdict:18} {name}\n{'':18} {detail}")
    bad = [r for r in results if r[1] != "RED (good)"]
    print(f"\n{len(results) - len(bad)}/{len(results)} mutations killed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
