"""ATOMIC-POSSESSIVE-BIND (issue #19 wound 2) — pins.

The owner ruling these pins enforce: L4 LINKS THE SUBJECT TO THE MEASUREMENT — the typed
atomic value (IP / MAC / email — the detector's existing value classes) hangs OFF the
POSSESSOR'S referent (the router), the model name files as the instance/alias row, the model
name NEVER becomes the atomic's value, and a scoped answer carries only on-topic rows (a
second subject's fact must not bleed in).

Battery (deploy-#10 rows, DEV/batteries/nine-subject.yaml):
  net3 "My router is a Unifi Dream Machine and its IP is 192.168.1.1."
       → was: NO (router, has_ip, 192.168.1.1) row; has_ip bound the MODEL NAME as the value
         ("You have IP address unifi dream machine"); AND the router answer rendered the
         RECIPE fact (cross-topic scope bleed).

Frame grammar (CGEL ch.17 §5 anaphora; ch.5 determinatives): the possessive-atomic copula
"<its/his/her> <attr-noun> is <ATOMIC value>" — a 3rd-person possessive determiner takes the
NEAREST PRECEDING-CLAUSE SUBJECT (same sentence) as its antecedent; the typed scalar binds
THERE. The compound-sentence mis-parse (spaCy hangs the second clause's subject under the
first clause's predicate as a conj) is recovered structurally at three seams: the connect
twins' absorbed-subject arm, the attr-scalar value-span firewall (a scalar literal never
contains a possessive determiner), and the conjunct-distribution guard.

Requires: SPACY_MODEL=en_core_web_sm and, for the E2E half, POSTGRES_DSN pointing ONLY at a
throwaway test DB (the same discipline as the df18/w1 suites).
"""
import json
import os
import time
import uuid

import pytest

os.environ.setdefault("SPACY_MODEL", "en_core_web_sm")
os.environ.setdefault("SENTENCE_PIPELINE", "true")

NET3 = "My router is a Unifi Dream Machine and its IP is 192.168.1.1."
NET3_Q = "What is my router's IP address?"
C1 = "My bouillabaisse recipe uses 800 grams of fish."      # the second subject (bleed probe)
LAPTOP = "My laptop is a ThinkPad and its MAC is AA:BB:CC:DD:EE:FF."
LAPTOP_Q = "What is my laptop's MAC address?"
DAD = "Dad's email is dad@example.org on the family account."
DAD_Q = "What is Dad's email address?"
UNCLE_HIS = "My uncle is a teacher and his email is u@example.org."
SISTER_HER = "My sister is a nurse and her email is s@example.org."
NEG = "My firewall is a Voxl and its name is Router."        # non-atomic object — no has_* mint

_DSN = os.environ.get("POSTGRES_DSN", "")
_E2E = bool(_DSN) and "test" in _DSN


def _facts(text):
    from src.extraction.linguistics import derive_sentence_facts
    return [(f.subject, f.rel_type, f.object)
            for f in derive_sentence_facts(text, None)]


def _possessor(text, value):
    from src.extraction.linguistics import possessor_of_possessive_attribute
    return possessor_of_possessive_attribute(text, value)


def _term(text, value):
    from src.extraction.linguistics import possessive_attribute_term
    return possessive_attribute_term(text, value)


# ── UNIT: the connect twins — clause-local binding + the pronoun antecedent ──────────────────────

def test_its_resolves_the_preceding_clause_subject():
    # the wound: bound to "user" (the FIRST clause's possessor) before the clause-local fix
    assert _possessor(NET3, "192.168.1.1") == "router"
    assert _term(NET3, "192.168.1.1") == ("ip", "ip")


def test_its_resolves_for_the_mac_variant():
    assert _possessor(LAPTOP, "AA:BB:CC:DD:EE:FF") == "laptop"
    assert _term(LAPTOP, "AA:BB:CC:DD:EE:FF") == ("mac", "mac")


def test_his_her_pronoun_possessors_resolve_the_same_way():
    assert _possessor(UNCLE_HIS, "u@example.org") == "uncle"
    assert _possessor(SISTER_HER, "s@example.org") == "sister"


def test_genitive_possessor_dads_email():
    # the EXISTING genitive branch, now clause-local — byte-for-byte for the single clause
    assert _possessor(DAD, "dad@example.org") == "dad"
    assert _term(DAD, "dad@example.org") == ("email", "email")


def test_single_clause_first_person_unchanged():
    assert _possessor("My phone number is 519-555-0123", "519-555-0123") == "user"
    assert _term("My email address is jordan.smith@example.com",
                 "jordan.smith@example.com") == ("email address", "address")


def test_cross_sentence_anaphora_stays_unbound():
    # the standing rule: cross-sentence anaphora is left unbound on purpose (a wrong bind
    # corrupts user truth) — fail-safe → None → the /ingest supplementation backstop
    assert _possessor("My router is a Unifi Dream Machine. Its IP is 192.168.1.1.",
                      "192.168.1.1") is None


def test_pronoun_antecedent_needs_a_preceding_subject():
    # no preceding clause subject → no bind (fail-safe, never a guess)
    assert _possessor("Its IP is 192.168.1.1.", "192.168.1.1") is None


# ── UNIT: the deriver — the weld/junk twins never mint ───────────────────────────────────────────

def test_no_coordination_glue_weld():
    rows = _facts(NET3)
    # the weld: (user, <attr-noun-as-rel>, "<model> and its <attr>") — the island that blocked
    # the typed scalar and canonicalized router→has_ip onto the junk value
    assert not any("and its" in (o or "") for s, r, o in rows)
    assert not any(r == "router" for s, r, o in rows)
    # the L4 links the subject to the measurement: owns + instance_of + the alias row
    assert ("user", "owns", "router") in rows
    assert ("router", "instance_of", "unifi dream machine") in rows


def test_no_junk_instance_placement_of_the_attr_noun():
    rows = _facts(LAPTOP)
    # the conjunct-distribution junk: the attribute noun typed as an instance_of rung
    assert not any(r == "instance_of" and o == "mac" for s, r, o in rows)
    assert ("laptop", "instance_of", "thinkpad") in rows
    assert ("user", "owns", "laptop") in rows


def test_negative_non_atomic_object_mints_no_atomic_rel():
    rows = _facts(NEG)
    assert not any(r in ("has_ip", "has_mac", "has_email") for s, r, o in rows)
    # the name lane owns "its name is Router" — never the atomic detector


def test_plain_classification_without_atomic_untouched():
    # the typed-atomic pre-pass gate still requires an atomic IN THE TURN: a plain
    # "my <X> is a <Type>" keeps today's attr-scalar behaviour (no owns/instance_of re-type)
    rows = _facts("My chapter draft is 3000 words.")
    assert ("user", "chapter_draft", "3000 words") in rows


# ── E2E: the battery through the REAL harvest → /ingest → /query ────────────────────────────────

_E2E_STATE: dict = {}


@pytest.mark.skipif(not _E2E, reason="POSTGRES_DSN not pointed at a throwaway test DB")
class TestE2EBattery:
    @pytest.fixture(autouse=True, scope="class")
    def _driver(self, request):
        import psycopg2
        import src.api.main as M
        from starlette.testclient import TestClient
        from src.provisioning.schema_manager import (
            create_user_schema, derive_user_slug_from_uuid)

        # conftest pins SAAS_MODE=1 for the session; the open-core harvest is FAIL-CLOSED
        # without a bound tenant brain — pop it for this class only (df18/w1 precedent).
        _had_saas = "SAAS_MODE" in os.environ
        _saas_val = os.environ.pop("SAAS_MODE", None)

        M._INGEST_ENABLED = True
        M.SPINE_DETERMINISTIC_SEGMENTATION = True

        class _D:
            def __init__(self): self.d = {}
            def get(self, k): return self.d.get(k)
            def setex(self, k, t, v): self.d[k] = v; return True
            def ping(self): return True

        class _Idem:
            redis_url = "dict://test"; ttl = 3600; client = _D()
        M._idempotency_mgr = _Idem()

        user_id = str(uuid.uuid4())
        slug = derive_user_slug_from_uuid(user_id)
        conn = psycopg2.connect(_DSN)
        schema, status = create_user_schema(user_id, slug, conn)
        assert status == "ready", status
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.users (user_id, email, display_name, slug) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, f"{slug}@example.invalid", slug, slug))
            cur.execute(
                "INSERT INTO public.user_provisioning (user_id, schema_name, status, ready_at) "
                "VALUES (%s, %s, 'ready', now()) ON CONFLICT DO NOTHING",
                (user_id, schema))
        conn.commit()
        conn.close()
        _E2E_STATE["schema"] = schema
        _E2E_STATE["user_id"] = user_id

        def rows(sql, params=()):
            c = psycopg2.connect(_DSN)
            try:
                with c.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}")
                    cur.execute(sql, params)
                    return cur.fetchall()
            finally:
                c.close()

        _E2E_STATE["rows"] = rows
        try:
            with TestClient(M.app) as client:
                from src.extraction import linguistics as _L
                t0 = time.time()
                while _L._nlp is None and time.time() - t0 < 120:
                    time.sleep(0.5)
                assert _L._nlp is not None
                _E2E_STATE["client"] = client
                # battery order: the SECOND subject first, so the bleed probe has something
                # off-topic in the same tenant before the router lands
                for text in (C1, NET3, LAPTOP, DAD, NEG):
                    h = client.post("/harvest-spans", json={"text": text, "user_id": user_id})
                    assert h.status_code == 200, h.text[:300]
                    r = client.post("/ingest", json={
                        "text": text, "user_id": user_id,
                        "edges": h.json().get("edges") or [], "source": "mcp"})
                    assert r.status_code == 200, r.text[:300]
                yield
        finally:
            if _had_saas:
                os.environ["SAAS_MODE"] = _saas_val

    def _attrs(self):
        return {(alias, attr, val) for alias, attr, val in _E2E_STATE["rows"](
            "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
            "a.entity_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), a.entity_id::text), "
            "a.attribute, a.value_text FROM entity_attributes a")}

    def _fact_rows(self):
        return _E2E_STATE["rows"](
            "SELECT COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
            "f.subject_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.subject_id::text), "
            "f.rel_type, "
            "COALESCE((SELECT alias FROM entity_aliases ea WHERE ea.entity_id = "
            "f.object_id ORDER BY ea.is_preferred DESC, ea.id LIMIT 1), f.object_id::text) "
            "FROM facts f WHERE f.superseded_at IS NULL AND f.archived_at IS NULL "
            "AND f.deleted_at IS NULL")

    def _query(self, q):
        r = _E2E_STATE["client"].post("/query", json={
            "text": q, "user_id": _E2E_STATE["user_id"]})
        assert r.status_code == 200, r.text[:300]
        return r.json()

    def test_router_ip_lands_on_the_router(self):
        attrs = self._attrs()
        assert ("router", "has_ip", "192.168.1.1") in attrs
        # the wound shape: the model name as the IP value / the IP on the USER
        assert not any(alias == "user" and attr == "has_ip" for alias, attr, val in attrs)
        assert not any("unifi" in (val or "") for alias, attr, val in attrs
                       if attr == "has_ip")

    def test_model_files_as_instance_and_alias_never_as_value(self):
        facts = {(s, r, o) for s, r, o in self._fact_rows()}
        assert any(s == "router" and r in ("instance_of", "is_a", "also_known_as",
                                           "related_to") and o == "unifi dream machine"
                   for s, r, o in facts)
        assert ("user", "owns", "router") in facts

    def test_scoped_router_answer_carries_the_ip_not_the_other_subject(self):
        j = self._query(NET3_Q)
        rendered = (j.get("response") or "") + " " + json.dumps(
            j.get("facts") or [], ensure_ascii=False)
        assert "192.168.1.1" in rendered
        assert "bouillabaisse" not in rendered.lower()      # the W2b not_contains pin
        assert any(f.get("rel_type") == "has_ip"
                   and f.get("object") == "192.168.1.1" for f in (j.get("facts") or []))

    def test_mac_variant_e2e(self):
        attrs = self._attrs()
        assert any(alias == "laptop" and attr == "has_mac"
                   and (val or "").lower() == "aa:bb:cc:dd:ee:ff"
                   for alias, attr, val in attrs)
        j = self._query(LAPTOP_Q)
        rendered = (j.get("response") or "") + " " + json.dumps(
            j.get("facts") or [], ensure_ascii=False)
        assert "aa:bb:cc:dd:ee:ff" in rendered.lower()
        assert "bouillabaisse" not in rendered.lower()

    def test_dads_email_e2e(self):
        attrs = self._attrs()
        assert any(alias == "dad" and attr == "has_email" and val == "dad@example.org"
                   for alias, attr, val in attrs)
        j = self._query(DAD_Q)
        rendered = (j.get("response") or "") + " " + json.dumps(
            j.get("facts") or [], ensure_ascii=False)
        assert "dad@example.org" in rendered

    def test_negative_no_atomic_minted_e2e(self):
        attrs = self._attrs()
        assert not any(attr in ("has_ip", "has_mac", "has_email")
                       and (val or "").strip().lower() in ("router", "voxl")
                       for alias, attr, val in attrs)
