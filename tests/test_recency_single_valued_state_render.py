"""Recency-picks-current at render for SINGLE-VALUED state rels
(DESIGN-memory-temporal-lifecycle §5).

Two coexisting UNDATED 'now' state values on a SINGLE-VALUED rel (residence, `lives_in`)
must render as current(latest)+FORMER(older) — "You live in Vancouver" + "Previously, you
live in Toronto" — instead of both-current. A MULTI-VALUED state rel (`likes`) must keep
ALL coexisting values current (cardinality guard). Query-side only; both values still
coexist in the store.

Runs against POSTGRES_DSN (reachable inside the faultline docker network as
postgres:5432). Skips cleanly if no DB. Throwaway schema, dropped in tearDown.
"""
import os
import uuid
import unittest

import psycopg2

DSN = os.environ.get("POSTGRES_DSN", "")
SCHEMA = "faultline_rectest"

USER = str(uuid.uuid5(uuid.NAMESPACE_DNS, "rectest-user"))
TORONTO = str(uuid.uuid5(uuid.NAMESPACE_DNS, "rectest-toronto"))
VANCOUVER = str(uuid.uuid5(uuid.NAMESPACE_DNS, "rectest-vancouver"))
PIZZA = str(uuid.uuid5(uuid.NAMESPACE_DNS, "rectest-pizza"))
SUSHI = str(uuid.uuid5(uuid.NAMESPACE_DNS, "rectest-sushi"))


def _dbok():
    if not DSN:
        return False
    try:
        psycopg2.connect(DSN).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_dbok(), "POSTGRES_DSN not reachable")
class RecencySingleValuedRenderTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        conn = psycopg2.connect(DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
            # rel_types: copy the public template (all render/overlay columns + templates),
            # then add the migration-168 cardinality column and seed residence single-valued.
            cur.execute(
                f"CREATE TABLE {SCHEMA}.rel_types (LIKE public.rel_types INCLUDING ALL)")
            cur.execute(
                f"INSERT INTO {SCHEMA}.rel_types SELECT * FROM public.rel_types")
            # IF NOT EXISTS: public.rel_types has carried the column since migration 168 was
            # applied to the template, so the LIKE copy already has it on a migrated db.
            cur.execute(
                f"ALTER TABLE {SCHEMA}.rel_types "
                f"ADD COLUMN IF NOT EXISTS is_single_valued BOOLEAN NOT NULL DEFAULT false")
            # residence = single-valued (one current home); `likes` stays multi-valued.
            cur.execute(
                f"UPDATE {SCHEMA}.rel_types SET is_single_valued = true "
                f"WHERE rel_type IN ('lives_in','lives_at')")
            # minimal facts table so _build_identity_set's same_as probe does not error.
            cur.execute(
                f"CREATE TABLE {SCHEMA}.facts ("
                f"  subject_id TEXT, object_id TEXT, rel_type TEXT,"
                f"  superseded_at TIMESTAMPTZ, archived_at TIMESTAMPTZ, deleted_at TIMESTAMPTZ)")
        conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = psycopg2.connect(DSN)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.close()

    def _prose(self, facts):
        """Bind the throwaway schema and render facts through the REAL convert_to_prose."""
        import src.api.main as m
        from src.api import rel_type_overlay
        rel_type_overlay.invalidate(None)
        tok = rel_type_overlay.set_current_schema(SCHEMA)
        conn = psycopg2.connect(DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
            pref = {TORONTO: "Toronto", VANCOUVER: "Vancouver",
                    PIZZA: "pizza", SUSHI: "sushi"}
            return m.convert_to_prose(
                facts, conn, anchor=USER, user_id=USER, preferred_alias_map=pref)
        finally:
            conn.close()
            rel_type_overlay.reset_current_schema(tok)
            rel_type_overlay.invalidate(None)

    def _fact(self, obj, rel, ingest_id, created_at):
        return {
            "subject": USER, "object": obj, "rel_type": rel,
            "confidence": 1.0, "fact_class": "A", "source": "db",
            "category": None, "valid_from": None, "valid_until": None,
            "temporal_status": "now", "event_date": None,
            "event_date_granularity": None, "polarity": "affirmed",
            "created_at": created_at, "_ingest_id": ingest_id,
        }

    # ── UNIT: cardinality branch on the helper directly (no DB dependency) ──
    def test_helper_single_vs_multi(self):
        import src.api.main as m
        meta = {
            "lives_in": {"temporal_class": "state", "is_single_valued": True},
            "likes": {"temporal_class": "state", "is_single_valued": False},
        }
        facts = [
            self._fact(TORONTO, "lives_in", 1, "2026-07-10T10:00:00"),
            self._fact(VANCOUVER, "lives_in", 2, "2026-07-11T10:00:00"),
            self._fact(PIZZA, "likes", 3, "2026-07-10T10:00:00"),
            self._fact(SUSHI, "likes", 4, "2026-07-11T10:00:00"),
        ]
        m._recency_collapse_single_valued_state(facts, lambda rt: meta.get(rt))
        by_obj = {f["object"]: f["temporal_status"] for f in facts}
        # single-valued: older Toronto → past, latest Vancouver stays now
        self.assertEqual(by_obj[TORONTO], "past")
        self.assertEqual(by_obj[VANCOUVER], "now")
        # multi-valued: BOTH likes stay now (cardinality guard)
        self.assertEqual(by_obj[PIZZA], "now")
        self.assertEqual(by_obj[SUSHI], "now")

    # ── PROSE: residence — Vancouver current, Toronto "Previously" ──
    def test_prose_residence_recency(self):
        facts = [
            self._fact(TORONTO, "lives_in", 1, "2026-07-10T10:00:00"),
            self._fact(VANCOUVER, "lives_in", 2, "2026-07-11T10:00:00"),
        ]
        lines = self._prose(facts)
        blob = " || ".join(lines)
        van = [l for l in lines if "Vancouver" in l]
        tor = [l for l in lines if "Toronto" in l]
        self.assertTrue(van, f"no Vancouver line: {lines}")
        self.assertTrue(tor, f"no Toronto line: {lines}")
        # older Toronto marked FORMER; latest Vancouver NOT former.
        self.assertTrue(tor[0].lower().startswith("previously")
                        or "used to" in tor[0].lower(),
                        f"Toronto not framed as former: {tor[0]!r}")
        self.assertFalse(van[0].lower().startswith("previously")
                         or "used to" in van[0].lower(),
                         f"Vancouver wrongly framed as former: {van[0]!r}")
        print("\nRESIDENCE PROSE:", blob)

    # ── PROSE: multi-valued — both likes current, neither "Previously" ──
    def test_prose_multivalued_kept(self):
        facts = [
            self._fact(PIZZA, "likes", 3, "2026-07-10T10:00:00"),
            self._fact(SUSHI, "likes", 4, "2026-07-11T10:00:00"),
        ]
        lines = self._prose(facts)
        blob = " || ".join(lines)
        self.assertTrue(any("pizza" in l.lower() for l in lines), f"pizza missing: {lines}")
        self.assertTrue(any("sushi" in l.lower() for l in lines), f"sushi missing: {lines}")
        self.assertFalse(any(l.lower().startswith("previously") or "used to" in l.lower()
                             for l in lines),
                         f"multi-valued wrongly marked former: {lines}")
        print("\nMULTI-VALUED PROSE:", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
