"""Italian deriver helper for the Italian engine tests (runs under it_core_news_sm).

The suite's own process loads the ENGLISH model (``_get_nlp`` caches one pipeline per process), so
Italian parses run here, in a child process with ``SPACY_MODEL=it_core_news_sm`` and
``FAULTLINE_LANGUAGE=it``. Input: argv[1] = mode, argv[2:] = sentences/queries. Output: the path of
a JSON result file on stdout (structlog noise goes elsewhere).

modes:
  facts   — derive_sentence_facts per sentence ("p1|p2::sentence" passes prior_nps) → [[{subject, rel_type, object, preferred_label}]]
  fp_poss — main._query_has_first_person_possessive per query → [bool]
  morph   — the parsed tokens' (text, dep, Person, Poss) per sentence
  guard   — file each surface as a user-asserted object, then ask the HARD-LINE ladder guard
  walk    — provision a throwaway tenant, file (anna, child_of, user), then determine_path per
            query → [{"rels": [...], "fp_poss": bool}]
"""
import datetime
import json
import logging
import os
import sys
import tempfile

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO)
os.environ["SPACY_MODEL"] = "it_core_news_sm"
os.environ["FAULTLINE_LANGUAGE"] = "it"
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:1")

logging.disable(logging.CRITICAL)
try:
    import structlog
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory())
except Exception:  # noqa: BLE001
    pass

mode = sys.argv[1]
items = sys.argv[2:]
out = []

if mode == "facts":
    from src.extraction import linguistics as m
    ref = datetime.datetime(2023, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)
    for s in items:
        # "prior1|prior2::sentence" threads prior-turn NPs (the deriver's _prior pool)
        prior = None
        if "::" in s:
            _p, s = s.split("::", 1)
            prior = [x for x in _p.split("|") if x]
        facts = m.derive_sentence_facts(s, ref, prior) or []
        out.append([{"subject": getattr(f, "subject", ""), "rel_type": getattr(f, "rel_type", ""),
                     "object": getattr(f, "object", ""),
                     "preferred_label": bool(getattr(f, "preferred_label", False))}
                    for f in facts])
elif mode == "morph":
    from src.extraction import linguistics as m
    for s in items:
        doc = m._parse(s)
        out.append([{"text": t.text, "dep": t.dep_, "person": t.morph.get("Person"),
                     "poss": t.morph.get("Poss")} for t in (doc or [])])
elif mode == "fp_poss":
    from src.api.main import _query_has_first_person_possessive
    out = [bool(_query_has_first_person_possessive(q)) for q in items]
elif mode == "walk":
    import uuid
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_schema_name, derive_user_slug_from_uuid)
    from src.api import rel_type_overlay
    from src.entity_registry.registry import EntityRegistry
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        create_user_schema(user_id, slug, conn)
        cur = conn.cursor()
        cur.execute("SET search_path TO " + schema)
        conn.commit()
        rel_type_overlay.set_current_schema(schema)
        reg = EntityRegistry(conn, auto_commit=True, schema_name=schema)
        user_uuid = reg.resolve(user_id, "user")
        anna = reg.resolve(user_id, "anna")
        cur.execute(
            "INSERT INTO facts (subject_id, object_id, rel_type, fact_provenance, confidence, "
            "fact_class, polarity) VALUES (%s, %s, 'child_of', 'user_stated', 1.0, 'A', 'affirmed') "
            "ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING", (anna, user_uuid))
        conn.commit()
        from src.api.main import determine_path, resolve_anchor, _query_has_first_person_possessive
        for q in items:
            a = resolve_anchor(q, [], user_id, conn, {})
            path = determine_path(q, conn, user_id=user_id, anchor_resolved_uuid=a)
            out.append({"rels": sorted(getattr(path, "relationship_rels", []) or []),
                        "fp_poss": bool(_query_has_first_person_possessive(q))})
    finally:
        try:
            c2 = conn.cursor()
            c2.execute("ROLLBACK")
            c2.execute("DROP SCHEMA IF EXISTS " + schema + " CASCADE")
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        conn.close()

if mode == "guard":
    # items: surfaces as the user wrote them. Each is filed as the OBJECT of a user_stated edge on a
    # novel verb-lemma rel (the shape the Italian SVO lane files: (user, avere, cane)), then the
    # HARD-LINE ladder guard is asked whether it may receive a subclass_of rung.
    import uuid
    import psycopg2
    from src.provisioning.schema_manager import (
        create_user_schema, derive_schema_name, derive_user_slug_from_uuid)
    from src.api import rel_type_overlay
    from src.api import hardline_guard
    from src.entity_registry.registry import EntityRegistry
    user_id = str(uuid.uuid4())
    slug = derive_user_slug_from_uuid(user_id)
    schema = derive_schema_name(slug)
    conn = psycopg2.connect(os.environ["POSTGRES_DSN"])
    try:
        create_user_schema(user_id, slug, conn)
        cur = conn.cursor()
        cur.execute("SET search_path TO " + schema)
        conn.commit()
        rel_type_overlay.set_current_schema(schema)
        reg = EntityRegistry(conn, auto_commit=True, schema_name=schema)
        user_uuid = reg.resolve(user_id, "user")
        from src.extraction.display_case import set_display_forms
        for item in items:
            # item grammar: "<surface>[@<entity_type>][><parent type>]" — "@" sets the node's ingest
            # type, ">" files a staged subclass_of rung to <parent> (the node already on the L4).
            surface, _, parent = item.partition(">")
            surface, _, etype = surface.partition("@")
            # the casing the ingest turn observed (a PROPN run keeps its capitals as display_form)
            set_display_forms({surface.lower(): surface} if surface != surface.lower() else {})
            eid = reg.resolve(user_id, surface.lower())
            if etype:
                cur.execute("UPDATE entities SET entity_type = %s WHERE id = %s", (etype, eid))
            if parent:
                pid = reg.resolve(user_id, parent.lower())
                cur.execute(
                    "INSERT INTO staged_facts (subject_id, object_id, rel_type, fact_class, "
                    "fact_provenance, confidence) VALUES (%s, %s, 'subclass_of', 'B', 'llm_learned', 0.6) "
                    "ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING", (eid, pid))
            conn.commit()
            cur.execute(
                "INSERT INTO staged_facts (subject_id, object_id, rel_type, fact_class, "
                "fact_provenance, confidence) VALUES (%s, %s, 'avere', 'B', 'user_stated', 0.8) "
                "ON CONFLICT (subject_id, object_id, rel_type) DO NOTHING", (user_uuid, eid))
            conn.commit()
            refuse, reason = hardline_guard.refuses_subclass_rung(
                conn, eid, surface.lower(), user_id=user_id)
            out.append({"surface": item, "refuse": bool(refuse), "reason": reason})
    finally:
        try:
            c2 = conn.cursor()
            c2.execute("ROLLBACK")
            c2.execute("DROP SCHEMA IF EXISTS " + schema + " CASCADE")
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        conn.close()

_f = os.path.join(tempfile.gettempdir(), f"it_capture_{os.getpid()}.json")
with open(_f, "w") as fh:
    fh.write(json.dumps(out))
print(_f)
