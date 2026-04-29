import os
import re
from contextlib import asynccontextmanager
import httpx
import psycopg2
import structlog
from fastapi import Depends, FastAPI, HTTPException
from src.fact_store.store import FactStoreManager
from src.re_embedder.embedder import derive_collection, embed_text, ensure_collection
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate, RelTypeRegistry
from .models import EdgeInput, EntityResult, FactResult, IngestRequest, IngestResponse, QueryRequest, RelTypeRequest

log = structlog.get_logger()

_gliner2_model = None
_rel_type_registry: RelTypeRegistry = None
_rel_type_constraint: str = ""

_PREFERENCE_SIGNALS = {
    "goes by", "go by",
    "prefers to be called", "prefer to be called",
    "preferred name", "my preferred name",
    "please call me", "call me",
    "known as", "also known as",
    "my name is", "i prefer", "i go by",
}

_IDENTITY_PATTERNS = [
    re.compile(r"\bmy name is ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bi am ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bi'm ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bcall me ([a-z]+)", re.IGNORECASE),
    re.compile(r"\bpeople call me ([a-z]+)", re.IGNORECASE),
]

_IDENTITY_STOPWORDS = {
    "a", "an", "the", "not", "just", "also", "here", "happy", "glad", "sorry",
    "married", "single", "divorced", "engaged", "here", "ready", "trying",
    "going", "looking", "back", "home", "out", "in", "on", "at", "to",
    "very", "really", "so", "too", "quite", "sure", "afraid", "aware",
    "excited", "sorry", "glad", "grateful", "proud", "tired", "done",
}

def _detect_preference_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(signal in text_lower for signal in _PREFERENCE_SIGNALS)

def _extract_identity(text: str) -> str | None:
    """Return the user's stated name if a self-identification pattern is found."""
    for pattern in _IDENTITY_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).lower().strip()
            if name not in _IDENTITY_STOPWORDS:
                return name
    return None

def _build_rel_type_constraint(dsn: str) -> str:
    """Load rel_types from DB and build pipe-separated bracket constraint."""
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT rel_type FROM rel_types ORDER BY rel_type")
                types = [row[0] for row in cur.fetchall()]
        return "|".join(types) if types else ""
    except Exception as e:
        log.warning("startup.constraint_builder_failed", error=str(e))
        return ""

def get_gliner_model():
    return _gliner2_model

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gliner2_model, _rel_type_registry, _rel_type_constraint

    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    default_collection = os.environ.get("QDRANT_COLLECTION", "faultline-test")
    log.info("startup.qdrant_collection_check", collection=default_collection)
    if ensure_collection(default_collection, qdrant_url):
        log.info("startup.qdrant_collection_ready", collection=default_collection)
    else:
        log.error("startup.qdrant_collection_failed", collection=default_collection)

    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        _rel_type_registry = RelTypeRegistry(dsn)
        try:
            _rel_type_registry.get_valid_types()
            _rel_type_constraint = _build_rel_type_constraint(dsn)
            log.info("startup.rel_type_registry_ready",
                     count=len(_rel_type_registry._cache),
                     constraint_len=len(_rel_type_constraint))
        except Exception as e:
            log.error("startup.rel_type_registry_failed", error=str(e))

    log.info("startup.gliner2_loading")
    try:
        from gliner2 import GLiNER2
        _gliner2_model = GLiNER2.from_pretrained("fastino/gliner2-base-v1")
        log.info("startup.gliner2_ready")
    except Exception as e:
        log.error("startup.gliner2_failed", error=str(e))
    yield
    _gliner2_model = None
    _rel_type_registry = None

app = FastAPI(title="FaultLine WGM", lifespan=lifespan)

@app.get("/health")
def health():
    if _gliner2_model is None:
        raise HTTPException(status_code=503, detail="Model loading")
    return {"status": "ok"}

@app.post("/ontology/rel_types")
def add_rel_type(req: RelTypeRequest):
    """
    User-asserted rel_type registration. Source is always 'user'.
    Wikidata and builtin types cannot be overwritten via this endpoint.
    """
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise HTTPException(status_code=503, detail="DB unavailable")
    try:
        with psycopg2.connect(dsn) as db:
            with db.cursor() as cur:
                # Trust hierarchy: user > wikidata > engine > builtin
                # Users can overwrite anything. Engine cannot overwrite user or wikidata.
                cur.execute(
                    "SELECT source FROM rel_types WHERE rel_type = %s",
                    (req.rel_type.lower(),),
                )
                existing = cur.fetchone()
                if existing and existing[0] == "user":
                    pass  # user-asserted types can be updated by users
                cur.execute(
                    "INSERT INTO rel_types"
                    " (rel_type, label, wikidata_pid, engine_generated, confidence, source,"
                    "  correction_behavior)"
                    " VALUES (%s, %s, %s, false, 1.0, 'user', %s)"
                    " ON CONFLICT (rel_type) DO UPDATE SET"
                    "   label = EXCLUDED.label,"
                    "   source = 'user',"
                    "   correction_behavior = EXCLUDED.correction_behavior",
                    (
                        req.rel_type.lower(),
                        req.label,
                        req.wikidata_pid,
                        req.correction_behavior,
                    ),
                )
                db.commit()
        if _rel_type_registry:
            _rel_type_registry._refresh()
        return {"status": "ok", "rel_type": req.rel_type.lower(), "source": "user"}
    except HTTPException:
        raise
    except Exception as e:
        log.error("ontology.add_rel_type_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/extract")
def extract(req: IngestRequest, model=Depends(get_gliner_model)):
    """
    Run GLiNER2 entity extraction only. Returns typed entities for use
    by the filter before calling Qwen for relationship classification.
    """
    if model is None:
        return {"entities": []}
    try:
        constraint = _rel_type_constraint or "is_a|parent_of|child_of|spouse|sibling_of|also_known_as|related_to|likes|dislikes|prefers|owns|knows|friend_of"
        schema = {
            "facts": [
                "subject::str::The full proper name of the first entity. Never a pronoun.",
                "object::str::The full proper name of the second entity. Never a pronoun.",
                f"rel_type::[{constraint}]::str::The relationship type from subject to object.",
                "subject_type::[Person|Animal|Organization|Location|Object|Concept]::str::The entity type of the subject.",
                "object_type::[Person|Animal|Organization|Location|Object|Concept]::str::The entity type of the object.",
            ]
        }
        result = model.extract_json(req.text, schema)
        return {"entities": result.get("facts", [])}
    except Exception as e:
        log.error("extract.gliner2_failed", error=str(e))
        return {"entities": []}

def _apply_correction(cur, user_id: str, old_value: str, new_value: str,
                      rel_type: str, new_fact_id: int, correction_behavior: str) -> int:
    if correction_behavior == "hard_delete":
        # DELETE stale alias facts BEFORE renaming subject (WHERE subject_id = old_value still matches)
        cur.execute(
            "DELETE FROM facts "
            "WHERE user_id = %s AND subject_id = %s AND id != %s AND rel_type = 'also_known_as'",
            (user_id, old_value, new_fact_id),
        )
        affected = cur.rowcount
        cur.execute(
            "UPDATE facts SET subject_id = %s, qdrant_synced = false "
            "WHERE user_id = %s AND subject_id = %s AND id != %s",
            (new_value, user_id, old_value, new_fact_id),
        )
        affected += cur.rowcount
        cur.execute(
            "UPDATE facts SET object_id = %s, qdrant_synced = false "
            "WHERE user_id = %s AND object_id = %s",
            (new_value, user_id, old_value),
        )
        affected += cur.rowcount
        return affected
    elif correction_behavior == "supersede":
        cur.execute(
            "UPDATE facts SET superseded_at = now(), qdrant_synced = false "
            "WHERE user_id = %s AND subject_id = %s AND rel_type = %s "
            "AND id != %s AND superseded_at IS NULL",
            (user_id, old_value, rel_type, new_fact_id),
        )
        return cur.rowcount
    else:  # immutable
        return 0


@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest, model=Depends(get_gliner_model)):
    inferred_relations = []
    if model is not None:
        try:
            constraint = _rel_type_constraint or "is_a|part_of|created_by|works_for|parent_of|child_of|spouse|sibling_of|also_known_as|related_to|likes|dislikes|prefers|has_gender|lives_in|born_in|born_on|nationality|educated_at|occupation|owns|located_in|knows|friend_of|met|age|located_at"
            schema = {
                "facts": [
                    "subject::str::The full proper name of the first entity in the relationship. Never a pronoun.",
                    "object::str::The full proper name of the second entity in the relationship. Never a pronoun.",
                    f"rel_type::[{constraint}]::str::The relationship type from subject to object. For 'X is a Y' where X is a named entity (person, place, thing), use instance_of. For 'X is a type of Y' where both are categories or classes, use subclass_of. Use pref_name for preferred display names, also_known_as for alternate names.",
                ]
            }
            result = model.extract_json(req.text, schema)
            raw_inferred = [
                EdgeInput(
                    subject=fact["subject"].lower().strip(),
                    object=fact["object"].lower().strip(),
                    rel_type=fact["rel_type"].lower().strip()
                )
                for fact in result.get("facts", [])
                if fact.get("subject") and fact.get("object") and fact.get("rel_type")
            ]

            # Build entity type map from GLiNER2 output for use in alias resolution
            # Only Person-type entities should have alias resolution applied
            _entity_types: dict[str, str] = {}
            for fact in result.get("facts", []):
                subj = fact.get("subject", "").lower().strip()
                obj = fact.get("object", "").lower().strip()
                if subj and fact.get("subject_type"):
                    _entity_types[subj] = fact["subject_type"].lower()
                if obj and fact.get("object_type"):
                    _entity_types[obj] = fact["object_type"].lower()

            # Build a set of parent_of pairs from this batch for directionality validation
            batch_parent_of = {
                (e.object, e.subject)  # (child, parent) — flipped for lookup
                for e in raw_inferred
                if e.rel_type == "parent_of"
            }

            inferred_relations = []
            for edge in raw_inferred:
                if edge.rel_type == "child_of":
                    # Only allow child_of(X, Y) if parent_of(Y, X) exists in this batch
                    # i.e. (subject=X, object=Y) requires (X, Y) in batch_parent_of
                    if (edge.subject, edge.object) not in batch_parent_of:
                        log.warning("ingest.child_of_rejected_no_parent",
                                    subject=edge.subject, object=edge.object)
                        continue
                inferred_relations.append(edge)
        except Exception as e:
            log.error("ingest.gliner2_failed", error=str(e))

    resolution = resolve_entities({"entities": []},
                                  context={"known_types": ["Person", "Organization", "Location"]})
    resolved = resolution["resolution"]["resolved"]

    edges_dict = {}
    for edge in (inferred_relations or []):
        key = (edge.subject, edge.object, edge.rel_type)
        edges_dict[key] = edge

    for edge in (req.edges or []):
        key = (edge.subject, edge.object, edge.rel_type)
        edges_dict[key] = edge

    # Auto-synthesize also_known_as if user identifies themselves
    detected_identity = _extract_identity(req.text)
    if detected_identity:
        identity_key = ("user", detected_identity, "also_known_as")
        if identity_key not in edges_dict:
            edges_dict[identity_key] = EdgeInput(
                subject="user",
                object=detected_identity,
                rel_type="also_known_as",
                is_preferred_label=True,
                is_correction=False,
            )

    edges = list(edges_dict.values())

    # Resolve "user" subject/object to known identity if established
    # Look up the most recent also_known_as fact for this user_id
    resolved_identity = None
    _dsn = os.environ.get("POSTGRES_DSN")
    try:
        if not _dsn:
            raise ValueError("No POSTGRES_DSN")
        _db = psycopg2.connect(_dsn)
        with _db.cursor() as _cur:
            _cur.execute(
                "SELECT object_id FROM facts "
                "WHERE user_id = %s AND subject_id = 'user' AND rel_type = 'also_known_as' "
                "ORDER BY id DESC LIMIT 1",
                (req.user_id,),
            )
            _row = _cur.fetchone()
            if _row:
                resolved_identity = _row[0]
        _db.close()
    except Exception as _e:
        log.warning("ingest.identity_resolution_failed", error=str(_e))

    if resolved_identity:
        resolved_edges = []
        for edge in edges:
            subj = resolved_identity if edge.subject == "user" else edge.subject
            obj = resolved_identity if edge.object == "user" else edge.object
            resolved_edges.append(EdgeInput(
                subject=subj,
                object=obj,
                rel_type=edge.rel_type,
                is_preferred_label=edge.is_preferred_label,
                is_correction=edge.is_correction,
            ))
        edges = resolved_edges

    # Build alias → canonical map from existing facts in DB
    # Only resolve Person-type entities to prevent cross-type contamination
    # e.g. don't resolve "sophia" (snake) even if it matches an alias
    _PERSON_TYPES = {"person", "per", "human", "character"}

    alias_to_canonical: dict[str, str] = {}
    _dsn2 = os.environ.get("POSTGRES_DSN")
    if _dsn2:
        try:
            _db2 = psycopg2.connect(_dsn2)
            with _db2.cursor() as _cur2:
                _cur2.execute(
                    "SELECT subject_id, object_id FROM facts "
                    "WHERE user_id = %s AND rel_type = 'also_known_as'",
                    (req.user_id,),
                )
                for canonical, alias in _cur2.fetchall():
                    alias_to_canonical[alias] = canonical
            _db2.close()
        except Exception as _e:
            log.warning("ingest.alias_resolution_failed", error=str(_e))

    def _is_person(name: str) -> bool:
        """Return True if entity is known to be a Person type."""
        entity_type = _entity_types.get(name, "")
        # If we have explicit type info, use it
        if entity_type:
            return entity_type in _PERSON_TYPES
        # If no type info, only resolve if it's a known alias
        # (conservative: unknown type entities are not resolved)
        return False

    if alias_to_canonical:
        resolved_alias_edges = []
        for edge in edges:
            # Only resolve subject/object if they are Person-type entities
            subj = alias_to_canonical[edge.subject] if (
                edge.subject in alias_to_canonical and _is_person(edge.subject)
            ) else edge.subject
            obj = alias_to_canonical[edge.object] if (
                edge.object in alias_to_canonical and _is_person(edge.object)
            ) else edge.object
            # Skip self-referential edges created by alias resolution
            if subj == obj:
                log.debug("ingest.alias_self_ref_skipped", subject=subj, rel=edge.rel_type)
                continue
            # Skip also_known_as edges where both sides resolve to same canonical
            if edge.rel_type == "also_known_as" and (
                alias_to_canonical.get(edge.subject) == alias_to_canonical.get(edge.object)
            ):
                continue
            resolved_alias_edges.append(EdgeInput(
                subject=subj,
                object=obj,
                rel_type=edge.rel_type,
                is_preferred_label=edge.is_preferred_label,
                is_correction=edge.is_correction,
            ))
        edges = resolved_alias_edges

    facts, committed = [], 0
    if edges:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        try:
            gate, manager = WGMValidationGate(db, _rel_type_registry), FactStoreManager(db)
            rows = []
            has_preferred = _detect_preference_signal(req.text)

            for edge in edges:
                if edge.subject == edge.object: continue
                status = gate.validate_edge(edge.subject, edge.object, edge.rel_type)["status"]
                facts.append(FactResult(subject=edge.subject, object=edge.object, rel_type=edge.rel_type, status=status))
                if status == "valid":
                    is_preferred = (
                        (edge.rel_type.lower() == "also_known_as" and
                         (has_preferred or edge.is_preferred_label))
                    )
                    rows.append((req.user_id, edge.subject, edge.object, edge.rel_type, req.source, is_preferred))

            if rows:
                committed = manager.commit(rows)

                correction_keys = {
                    (e.subject.lower(), e.object.lower(), e.rel_type.lower())
                    for e in edges if e.is_correction
                }

                with db.cursor() as cur:
                    for row in rows:
                        user_id, subject, obj, rel_type, source, is_preferred = row
                        if rel_type.lower() == "also_known_as" and is_preferred:
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = false"
                                " WHERE user_id = %s AND subject_id = %s AND rel_type = 'also_known_as'"
                                " AND object_id != %s",
                                (user_id, subject, obj),
                            )

                    for row in rows:
                        user_id, subject, obj, rel_type, source, is_preferred = row
                        if (subject.lower(), obj.lower(), rel_type.lower()) not in correction_keys:
                            continue
                        cur.execute(
                            "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                            " AND object_id = %s AND rel_type = %s",
                            (user_id, subject.lower(), obj.lower(), rel_type.lower()),
                        )
                        result = cur.fetchone()
                        if not result:
                            continue
                        new_fact_id = result[0]
                        cur.execute(
                            "SELECT correction_behavior FROM rel_types WHERE rel_type = %s",
                            (rel_type.lower(),),
                        )
                        cb_row = cur.fetchone()
                        behavior = cb_row[0] if cb_row else "supersede"
                        _apply_correction(cur, user_id, subject.lower(), obj.lower(),
                                          rel_type.lower(), new_fact_id, behavior)
                        if rel_type.lower() == "also_known_as":
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = true WHERE id = %s",
                                (new_fact_id,),
                            )

                    db.commit()
        finally: db.close()

    return IngestResponse(status="valid", committed=committed, 
                          entities=[EntityResult(entity=r["entity"], label=r["type"], canonical_id=r["canonical_id"]) for r in resolved], 
                          facts=facts)

@app.post("/query")
def query(request: QueryRequest):
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    qwen_api_url = os.environ.get("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
    user_id = request.user_id or "anonymous"
    collection = derive_collection(user_id)

    # Use a short embed timeout so the filter's request doesn't time out waiting for us.
    # fallback=False: if nomic is unavailable, return empty rather than searching with a
    # hash vector that cannot match the nomic-embedded stored facts.
    vector = embed_text(request.text, qwen_api_url, timeout=10.0, fallback=False)
    if vector is None:
        log.warning("query.embed_unavailable — skipping Qdrant search")
        return {"status": "ok", "facts": [], "preferred_names": {}}

    preferred_names = {}
    try:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        with db.cursor() as cur:
            cur.execute(
                "SELECT subject_id, object_id FROM facts"
                " WHERE user_id = %s AND rel_type = 'also_known_as' AND is_preferred_label = true",
                (user_id,),
            )
            preferred_names = {row[0]: row[1] for row in cur.fetchall()}
        db.close()
    except Exception as e:
        log.warning("query.preferred_names_error", error=str(e))

    try:
        resp = httpx.post(
            f"{qdrant_url}/collections/{collection}/points/search",
            json={"vector": vector, "limit": 10, "with_payload": True, "score_threshold": 0.3},
            timeout=10.0,
        )
        if resp.status_code == 404:
            ensure_collection(collection, qdrant_url)
            return {"status": "ok", "facts": [], "preferred_names": preferred_names}
        if resp.status_code != 200:
            log.warning("query.qdrant_error", status=resp.status_code, collection=collection)
            return {"status": "ok", "facts": [], "preferred_names": preferred_names}

        facts = [
            {
                "subject": h["payload"].get("subject"),
                "object": h["payload"].get("object"),
                "rel_type": h["payload"].get("rel_type"),
                "provenance": h["payload"].get("provenance"),
            }
            for h in resp.json().get("result", [])
            if h.get("payload")
        ]
        log.info("query.ok", collection=collection, hits=len(facts))
        return {"status": "ok", "facts": facts, "preferred_names": preferred_names}
    except Exception as e:
        log.error("query.failed", error=str(e))
        return {"status": "ok", "facts": [], "preferred_names": preferred_names}