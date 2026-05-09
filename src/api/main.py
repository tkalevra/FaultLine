import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import Optional
import httpx
import psycopg2
import structlog
from fastapi import Depends, FastAPI, HTTPException
from src.entity_registry.registry import EntityRegistry
from src.fact_store.store import FactStoreManager
from src.re_embedder.embedder import derive_collection, embed_text, ensure_collection, mark_synced, upsert_to_qdrant
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate, RelTypeRegistry
from .models import EdgeInput, EntityResult, FactResult, IngestRequest, IngestResponse, QueryRequest, RelTypeRequest, RetractRequest, RetractResponse, StoreContextRequest, StoreContextResponse

log = structlog.get_logger()

_gliner2_model = None
_rel_type_registry: RelTypeRegistry = None
_rel_type_constraint: str = ""
_REL_TYPE_META: dict = {}

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

# _SCALAR_REL_TYPES removed — replaced by classify_fact_type() which uses
# value-driven heuristics + DB-driven ontology hints (rel_types.tail_types).

_UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)

# Fact class taxonomy — determines write path at ingest time
_CLASS_A_REL_TYPES = frozenset({
    "pref_name", "also_known_as", "same_as",
    "parent_of", "child_of", "spouse", "sibling_of",
    "born_on", "born_in", "has_gender", "nationality",
    "instance_of", "subclass_of", "age", "height", "weight",
})

_CLASS_B_REL_TYPES = frozenset({
    "lives_at", "lives_in", "works_for", "occupation",
    "educated_at", "owns", "likes", "dislikes", "prefers",
    "friend_of", "knows", "met", "located_in",
    "related_to", "has_pet", "part_of", "created_by",
})

_VALID_CATEGORIES = frozenset({
    "physical", "temporal", "location", "work", "family", "pets", "identity"
})

# Class C = anything not in A or B, engine_generated types,
# novel types, or confidence < 0.6

def _classify_fact(
    rel_type: str,
    confidence: float,
    engine_generated: bool = False,
    is_correction: bool = False,
) -> str:
    """
    Classify a fact as A, B, or C based on rel_type, confidence, and provenance.

    Class A: Identity/structural facts — write-through to PostgreSQL immediately.
    Class B: Behavioral/contextual facts — staged, promote on confirmation.
    Class C: Ephemeral/novel facts — staged, expire after 30 days.

    Corrections from the user are always promoted to Class A regardless of rel_type.
    Engine-generated (novel) types are always Class C regardless of rel_type.
    """
    if is_correction:
        return "A"
    if engine_generated or confidence < 0.6:
        return "C"
    rt = rel_type.lower().strip()
    if rt in _CLASS_A_REL_TYPES:
        return "A"
    if rt in _CLASS_B_REL_TYPES:
        return "B"
    return "C"

def _infer_category(rel_type: str) -> str | None:
    """
    Keyword-based category inference — offline fallback only.
    Used when LLM is unavailable or returns an invalid category.
    """
    rt = rel_type.lower()
    if any(k in rt for k in ("height","weight","gender","age","physical","body")):
        return "physical"
    if any(k in rt for k in ("born","birth","anniversary","met_on","married_on")):
        return "temporal"
    if any(k in rt for k in ("live","address","location","city","home","reside")):
        return "location"
    if any(k in rt for k in ("work","job","employ","occupation","career")):
        return "work"
    if any(k in rt for k in ("parent","child","spouse","sibling","family")):
        return "family"
    if any(k in rt for k in ("pet","animal","dog","cat","fish","bird")):
        return "pets"
    if any(k in rt for k in ("name","alias","known","called","pref")):
        return "identity"
    return None

def _assign_category_via_llm(rel_type: str, qwen_api_url: str) -> Optional[str]:
    """
    Ask Qwen to assign a category to a novel rel_type.
    Returns a valid category string or None on failure.
    Falls back to _infer_category on invalid/empty response.
    """
    try:
        resp = httpx.post(
            qwen_api_url,
            json={
                "model": os.getenv("CATEGORY_LLM_MODEL", "qwen2.5-coder"),
                "messages": [{
                    "role": "user",
                    "content": (
                        f"What category does the relationship type '{rel_type}' belong to? "
                        f"Choose exactly one from this list: "
                        f"physical, temporal, location, work, family, pets, identity. "
                        f"Return only the single category word, nothing else. "
                        f"If none fit, return 'other'."
                    )
                }],
                "temperature": 0.0,
                "max_tokens": 10,
                "thinking": {"type": "disabled"},
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"].strip().lower()
            if raw in _VALID_CATEGORIES:
                return raw
    except Exception:
        pass
    return _infer_category(rel_type)

def _coerce_scalar(value: str) -> tuple:
    """
    Coerce a scalar value string to (value_text, value_int, value_float, value_date).
    Returns appropriate typed value and None for others.
    """
    # Try integer
    try:
        return (None, int(value), None, None)
    except ValueError:
        pass
    # Try float
    try:
        return (None, None, float(value), None)
    except ValueError:
        pass
    # Try date (basic YYYY-MM-DD)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        return (None, None, None, value)
    # Fall back to text
    return (value, None, None, None)

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

def _resolve_user_anchor(entity_id: str, user_id: str) -> str:
    """Return the canonical user UUID if entity_id matches, else return entity_id."""
    return user_id if entity_id == user_id else entity_id

# Emergency fallback constraint used only when DB is completely unreachable.
# Never used as the primary source — the DB rel_types table is authoritative.
_EMERGENCY_CONSTRAINT = (
    "instance_of|subclass_of|part_of|created_by|works_for|"
    "parent_of|child_of|spouse|sibling_of|also_known_as|pref_name|same_as|"
    "related_to|likes|dislikes|prefers|owns|located_in|educated_at|"
    "nationality|occupation|born_on|age|knows|friend_of|met|"
    "lives_in|born_in|has_gender|has_pet|lives_at|located_at|height|weight"
)

def _get_constraint() -> str:
    """
    Return the rel_type constraint string for GLiNER2 schemas.
    Uses the startup-built cache if populated; otherwise rebuilds from DB.
    The emergency fallback is only used when both are unavailable.
    """
    global _rel_type_constraint
    if _rel_type_constraint:
        return _rel_type_constraint
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        constraint = _build_rel_type_constraint(dsn)
        if constraint:
            _rel_type_constraint = constraint
            return constraint
    return _EMERGENCY_CONSTRAINT

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

def _build_rel_type_meta(dsn: str) -> dict:
    """Load rel_types metadata (including category) from DB."""
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT rel_type, category FROM rel_types WHERE category IS NOT NULL")
                meta = {}
                for rel_type, category in cur.fetchall():
                    meta[rel_type] = {"category": category}
        return meta
    except Exception as e:
        log.warning("startup.rel_type_meta_builder_failed", error=str(e))
        return {}

def _commit_staged(
    db_conn,
    rows: list[tuple],
    fact_class: str,
    confidence: float,
) -> int:
    """
    Insert or update rows in staged_facts.
    rows: list of (user_id, subject_id, object_id, rel_type, provenance)
    On conflict, increments confirmed_count and refreshes last_seen_at and expires_at.
    Returns count of rows attempted.
    """
    count = 0
    try:
        with db_conn.cursor() as cur:
            for user_id, subject, obj, rel_type, prov in rows:
                cur.execute(
                    "INSERT INTO staged_facts"
                    " (user_id, subject_id, object_id, rel_type, fact_class,"
                    "  provenance, confidence, expires_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '30 days')"
                    " ON CONFLICT (user_id, subject_id, object_id, rel_type)"
                    " DO UPDATE SET"
                    "   confirmed_count = staged_facts.confirmed_count + 1,"
                    "   last_seen_at    = now(),"
                    "   expires_at      = now() + interval '30 days',"
                    "   confidence      = GREATEST(staged_facts.confidence, EXCLUDED.confidence),"
                    "   qdrant_synced   = false",
                    (user_id, subject, obj, rel_type, fact_class, prov, confidence),
                )
                count += 1
        db_conn.commit()
        return count
    except Exception as e:
        db_conn.rollback()
        log.error("ingest.staged_commit_failed",
                  err=str(e),
                  row_count=len(rows),
                  fact_class=fact_class,
                  first_row=str(rows[0]) if rows else "empty")
        return 0

def get_gliner_model():
    return _gliner2_model

def _normalize_entity_ids_startup(dsn: str) -> None:
    """
    Normalize string entity_ids to UUID v5 surrogates at startup.
    This is idempotent and safe to run repeatedly.

    Scans facts/staged_facts for non-UUID entity_ids and converts them
    using EntityRegistry._make_surrogate() logic.
    """
    try:
        from src.entity_registry.registry import _make_surrogate

        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # Find all string entity_ids
                cur.execute("""
                    SELECT DISTINCT user_id, subject_id FROM facts
                    WHERE subject_id NOT LIKE '%-%-%-%-'
                    UNION
                    SELECT DISTINCT user_id, object_id FROM facts
                    WHERE object_id NOT LIKE '%-%-%-%-'
                    UNION
                    SELECT DISTINCT user_id, subject_id FROM staged_facts
                    WHERE subject_id NOT LIKE '%-%-%-%-'
                    UNION
                    SELECT DISTINCT user_id, object_id FROM staged_facts
                    WHERE object_id NOT LIKE '%-%-%-%-'
                """)
                string_ids = cur.fetchall()

                if not string_ids:
                    log.info("startup.entity_id_normalization_check", status="ok")
                    return

                log.info("startup.entity_id_normalization_starting",
                         string_id_count=len(string_ids))

                # Build mapping from (user_id, string_id) -> surrogate UUID
                entity_map = {}
                for user_id, string_id in string_ids:
                    if not _UUID_PATTERN.match(string_id):
                        surrogate = _make_surrogate(user_id, string_id)
                        entity_map[(user_id, string_id)] = surrogate

                # Update facts table
                updated_count = 0
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "UPDATE facts SET subject_id = %s WHERE user_id = %s AND subject_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                    cur.execute(
                        "UPDATE facts SET object_id = %s WHERE user_id = %s AND object_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                # Update staged_facts table
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "UPDATE staged_facts SET subject_id = %s WHERE user_id = %s AND subject_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                    cur.execute(
                        "UPDATE staged_facts SET object_id = %s WHERE user_id = %s AND object_id = %s",
                        (surrogate, user_id, string_id)
                    )
                    updated_count += cur.rowcount

                # Ensure entities are registered
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "INSERT INTO entities (id, user_id, entity_type) VALUES (%s, %s, 'unknown') "
                        "ON CONFLICT (id, user_id) DO NOTHING",
                        (surrogate, user_id)
                    )

                # Sync aliases
                for (user_id, string_id), surrogate in entity_map.items():
                    cur.execute(
                        "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                        "VALUES (%s, %s, %s, true) ON CONFLICT (user_id, alias) DO UPDATE SET entity_id = EXCLUDED.entity_id, is_preferred = EXCLUDED.is_preferred",
                        (surrogate, user_id, string_id.lower())
                    )

                conn.commit()
                log.info("startup.entity_id_normalization_complete",
                         string_ids_processed=len(entity_map),
                         rows_updated=updated_count)
    except Exception as e:
        log.error("startup.entity_id_normalization_failed", error=str(e))


def _ensure_schema(dsn: str) -> None:
    """
    Apply any pending schema migrations at startup.
    This is the idempotent equivalent of running migration SQL files —
    column renames, type changes, seed data. Each statement uses IF EXISTS
    or information_schema checks so it's safe to run on any DB state.
    """
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                # Rename allowed_head → head_types if the old column exists
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'rel_types' AND column_name = 'allowed_head'"
                )
                if cur.fetchone():
                    cur.execute("ALTER TABLE rel_types RENAME COLUMN allowed_head TO head_types")
                    log.info("startup.schema_rename", old="allowed_head", new="head_types")

                # Rename allowed_tail → tail_types if the old column exists
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'rel_types' AND column_name = 'allowed_tail'"
                )
                if cur.fetchone():
                    cur.execute("ALTER TABLE rel_types RENAME COLUMN allowed_tail TO tail_types")
                    log.info("startup.schema_rename", old="allowed_tail", new="tail_types")

                # Alter staged_facts.user_id from UUID to TEXT (conditional)
                cur.execute(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'staged_facts' AND column_name = 'user_id'"
                )
                row = cur.fetchone()
                if row and row[0].upper() == 'UUID':
                    cur.execute("ALTER TABLE staged_facts ALTER COLUMN user_id TYPE TEXT")
                    cur.execute("ALTER TABLE staged_facts ALTER COLUMN subject_id TYPE TEXT")
                    cur.execute("ALTER TABLE staged_facts ALTER COLUMN object_id TYPE TEXT")
                    log.info("startup.schema_staged_facts_uuid_to_text")

                # Seed missing rel_types that are referenced in code but might
                # not exist in the DB (safe — ON CONFLICT DO NOTHING)
                _MISSING_TYPES = [
                    ("lives_at", "Lives At", "location", "supersede"),
                    ("located_at", "Located At", "location", "supersede"),
                    ("has_pet", "Has Pet", "pets", "supersede"),
                    ("height", "Height", "physical", "supersede"),
                    ("weight", "Weight", "physical", "supersede"),
                ]
                for rel_type, label, category, correction_behavior in _MISSING_TYPES:
                    cur.execute(
                        "INSERT INTO rel_types (rel_type, label, category, correction_behavior, source) "
                        "VALUES (%s, %s, %s, %s, 'builtin') "
                        "ON CONFLICT (rel_type) DO NOTHING",
                        (rel_type, label, category, correction_behavior),
                    )
                conn.commit()
                log.info("startup.schema_check_complete")
    except Exception as e:
        log.warning("startup.schema_check_failed", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gliner2_model, _rel_type_registry, _rel_type_constraint, _REL_TYPE_META

    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    default_collection = os.environ.get("QDRANT_COLLECTION", "faultline-test")
    log.info("startup.qdrant_collection_check", collection=default_collection)
    if ensure_collection(default_collection, qdrant_url):
        log.info("startup.qdrant_collection_ready", collection=default_collection)
    else:
        log.error("startup.qdrant_collection_failed", collection=default_collection)

    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        _ensure_schema(dsn)  # apply pending migrations before reading the schema
        _normalize_entity_ids_startup(dsn)  # normalize string entity_ids to UUIDs
        _rel_type_registry = RelTypeRegistry(dsn)
        try:
            _rel_type_registry.get_valid_types()
            _rel_type_constraint = _build_rel_type_constraint(dsn)
            _REL_TYPE_META = _build_rel_type_meta(dsn)
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
    Post-processes to resolve first-person references (e.g., null objects
    for child_of relations where user is the implied parent).
    """
    if model is None:
        return {"entities": []}
    try:
        constraint = _get_constraint()
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
        resolved_entities = []
        for entity in result.get("facts", []):
            # Post-process: if object is null but subject is a person and rel_type implies parent relationship
            if entity.get("object") is None and entity.get("rel_type") == "child_of":
                # The user is the implied parent; flip the relationship
                entity["rel_type"] = "parent_of"
                entity["object"] = entity.get("subject")
                entity["object_type"] = entity.get("subject_type")
                entity["subject"] = "user"
                entity["subject_type"] = "Person"
                log.info("extract.null_object_resolved", subject=entity["object"], rel_type="parent_of")
            resolved_entities.append(entity)
        return {"entities": resolved_entities}
    except Exception as e:
        log.error("extract.gliner2_failed", error=str(e))
        return {"entities": []}

def _delete_from_qdrant(fact_ids: list[int], collection: str, qdrant_url: str) -> None:
    try:
        resp = httpx.delete(
            f"{qdrant_url}/collections/{collection}/points",
            json={"points": fact_ids},
            timeout=5.0,
        )
        if resp.status_code not in (200, 404):
            log.warning("qdrant.cleanup_partial", status=resp.status_code, count=len(fact_ids))
    except Exception as e:
        log.warning("qdrant.cleanup_failed", error=str(e), count=len(fact_ids))

def _extract_pet_descriptor(text: str, pet_name: str) -> Optional[dict]:
    """
    Extract pet descriptors (breed, species, color, size) from text context.
    Pattern: "have/own a [DESCRIPTOR]+ [PET_NAME]" or "[PET_NAME] is a [DESCRIPTOR]+"
    Returns dict with descriptor type (species/breed/color/size) and value.
    """
    patterns = [
        # "have a [species/breed] [color]? [name]"
        (r"(?:have|own|got|has)\s+(?:a|an|my)\s+([a-z\s]+?)\s+(?:named|called|named)\s+" + re.escape(pet_name.lower()) + r"\b", "species"),
        # "[name] is a [species/breed]"
        (re.escape(pet_name.lower()) + r"\s+is\s+(?:a|an)\s+([a-z\s]+?)(?:\band\b|$)", "species"),
        # "[name], a [descriptor]"
        (re.escape(pet_name.lower()) + r",\s+(?:a|an)\s+([a-z\s]+?)(?:,|named|$)", "species"),
    ]

    text_lower = text.lower()
    descriptors = []

    for pattern, descriptor_type in patterns:
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            raw_descriptor = match.group(1).strip().lower()
            # Clean up descriptor: remove trailing conjunctions, articles, etc.
            raw_descriptor = re.sub(r"\s+(and|or|named|called).*$", "", raw_descriptor)
            raw_descriptor = raw_descriptor.strip()
            if raw_descriptor and len(raw_descriptor) < 50:  # sanity check
                descriptors.append({
                    "type": descriptor_type,
                    "value": raw_descriptor
                })

    return descriptors if descriptors else None


def _infer_type_from_relationship(rel_type: str, entity_position: str) -> Optional[str]:
    """
    Infer entity type from relationship semantics.
    Used as fallback when GLiNER2 classification is uncertain or conflicts with context.
    entity_position: 'subject' or 'object'
    """
    relationship_hints = {
        "has_pet": {"object": "Animal"},
        "parent_of": {"subject": "Person", "object": "Person"},
        "child_of": {"subject": "Person", "object": "Person"},
        "spouse": {"subject": "Person", "object": "Person"},
        "sibling_of": {"subject": "Person", "object": "Person"},
        "works_for": {"subject": "Person", "object": "Organization"},
        "lives_at": {"subject": "Person", "object": "Location"},
        "lives_in": {"subject": "Person", "object": "Location"},
        "located_in": {"object": "Location"},
        "born_in": {"object": "Location"},
        "created_by": {"subject": "Person"},
        "part_of": {"object": "Organization"},
    }

    rel_hints = relationship_hints.get(rel_type.lower())
    if rel_hints:
        return rel_hints.get(entity_position)
    return None


def classify_fact_type(
    rel_type: str,
    object_value: str,
    registry,  # EntityRegistry instance
    user_id: str,
) -> dict:
    """
    Dynamically classify whether a fact's object is a scalar value or an entity
    reference. Replaces the hardcoded _SCALAR_REL_TYPES set.

    Layered heuristics, evaluated in order, first match wins:

        L0  same_as rel_type                   → relationship (semantic constant)
        L1  Integer / float / % / IP / currency / measurement / height  → scalar
        L2  ISO date / slash date / year / month+day                    → scalar
        L3  UUID pattern                                                → relationship
        L4  entity_aliases lookup (known name)                           → relationship
        L5  Email / URL / phone / long text / value-indicator / capital  → mixed
        L6  rel_types.tail_types from DB ontology                        → mixed
        L7  Fallback                                                    → uncertain

    Returns:
        {"type": "scalar" | "relationship" | "uncertain",
         "confidence": float (0.0–1.0),
         "reason": str}
    """
    import re

    stripped = object_value.strip()
    if not stripped:
        return {"type": "uncertain", "confidence": 0.0, "reason": "empty value"}

    stripped_lower = stripped.lower()
    rt_lower = rel_type.lower()

    # L0 — Semantic constant: same_as is owl:sameAs (entity identity, both UUIDs)
    if rt_lower == "same_as":
        return {"type": "relationship", "confidence": 1.0,
                "reason": "same_as is definitionally entity→entity"}

    # L1 — Numeric / technical patterns  (confidence ≥ 0.95)
    if re.match(r'^-?\d+$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "integer pattern"}
    if re.match(r'^-?\d+\.\d+$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "float pattern"}
    if re.match(r'^-?\d+(\.\d+)?%$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "percentage pattern"}
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "IPv4 address pattern"}
    if re.match(r'^[\$\£\€\¥]\d{1,3}(,\d{3})*(\.\d{2})?$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "currency pattern"}
    if re.match(r'^\d+(\.\d+)?\s*(cm|kg|lbs?|pounds?|ft|feet|inch(?:es)?|'
                r'miles?|km|mph?|kph?|meters?|metres?|grams?|ounces?|oz)$',
                stripped_lower):
        return {"type": "scalar", "confidence": 0.95, "reason": "measurement with unit"}
    if re.match(r"^\d+\s*['\"]?\s*\d*\s*[\"]?\s*$", stripped):
        return {"type": "scalar", "confidence": 0.95, "reason": "height measurement (ft/in)"}

    # L2 — Date / time patterns  (confidence ≥ 0.85)
    if re.match(r'^\d{4}-\d{2}-\d{2}$', stripped):
        return {"type": "scalar", "confidence": 0.95, "reason": "ISO date (YYYY-MM-DD)"}
    if re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?', stripped):
        return {"type": "scalar", "confidence": 0.95, "reason": "ISO datetime pattern"}
    if re.match(r'^\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}$', stripped):
        return {"type": "scalar", "confidence": 0.90, "reason": "slash-date pattern"}
    if re.match(r'^(19|20)\d{2}$', stripped):
        return {"type": "scalar", "confidence": 0.85, "reason": "year-only pattern"}
    _MONTH_RE = (r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|'
                 r'may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|'
                 r'oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)')
    if re.match(rf'^{_MONTH_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?$', stripped_lower):
        return {"type": "scalar", "confidence": 0.90, "reason": "month-name + day pattern"}

    # L3 — UUID pattern  (definitive relationship)
    if _UUID_PATTERN.match(stripped):
        return {"type": "relationship", "confidence": 0.98, "reason": "UUID pattern"}

    # L4 — Entity alias lookup  (DB: entity_aliases)
    try:
        with registry.db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM entity_aliases WHERE user_id = %s AND alias = %s LIMIT 1",
                (user_id, stripped_lower),
            )
            if cur.fetchone():
                return {"type": "relationship", "confidence": 0.90, "reason": "known entity alias"}
    except Exception:
        pass

    # L5 — String-pattern heuristics
    word_count = len(stripped.split())
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', stripped):
        return {"type": "scalar", "confidence": 0.98, "reason": "email pattern"}
    if stripped_lower.startswith(('http://', 'https://', 'www.', 'ftp://')):
        return {"type": "scalar", "confidence": 0.98, "reason": "URL pattern"}
    if re.match(r'^\+?[\d\s\-\(\)\.]{7,}$', stripped):
        return {"type": "scalar", "confidence": 0.90, "reason": "phone-number pattern"}
    if word_count >= 5:
        return {"type": "scalar", "confidence": 0.80, "reason": f"descriptive string ({word_count} words)"}
    _VALUE_INDICATORS = frozenset({
        "street", "road", "avenue", "lane", "drive", "boulevard",
        "court", "place", "highway", "circle", "square",
        "engineer", "doctor", "teacher", "student", "manager",
        "director", "president", "ceo", "cto", "cfo",
        "professor", "nurse", "lawyer", "artist", "writer",
        "consultant", "analyst", "developer", "designer", "scientist",
    })
    if any(word in stripped_lower for word in _VALUE_INDICATORS):
        return {"type": "scalar", "confidence": 0.75, "reason": "value-indicator term detected"}
    if word_count == 1 and stripped[0].isupper():
        return {"type": "relationship", "confidence": 0.70, "reason": "single capitalized word (probable name)"}

    # L6 — DB-driven rel-type ontology hints  (tail_types from rel_types)
    if _rel_type_registry is not None:
        try:
            rt_meta = _rel_type_registry.get(rt_lower, {})
            tail_types = rt_meta.get("tail_types")
            if tail_types == ["SCALAR"]:
                return {"type": "scalar", "confidence": 0.90, "reason": "rel_type ontology: tail_types=SCALAR"}
            if (tail_types and tail_types != ["ANY"]
                    and "ANY" not in tail_types
                    and "SCALAR" not in tail_types):
                return {"type": "relationship", "confidence": 0.85,
                        "reason": f"rel_type ontology: tail_types={tail_types}"}
        except Exception:
            pass

    # L7 — Fallback
    return {"type": "uncertain", "confidence": 0.50,
            "reason": "ambiguous — no pattern or ontology hint matched"}


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
    if model is not None and not req.edges:
        try:
            constraint = _get_constraint()
            schema = {
                "facts": [
                    "subject::str::The full proper name of the first entity in the relationship. Never a pronoun.",
                    "subject_type::[Person|Animal|Organization|Location|Object|Concept]::str::The semantic type of the subject entity.",
                    "object::str::The full proper name of the second entity in the relationship. Never a pronoun.",
                    "object_type::[Person|Animal|Organization|Location|Object|Concept]::str::The semantic type of the object entity.",
                    f"rel_type::[{constraint}]::str::The relationship type from subject to object. For 'X is a Y' where X is a named entity (person, place, thing), use instance_of. For 'X is a type of Y' where both are categories or classes, use subclass_of. Use pref_name for preferred display names, also_known_as for alternate names.",
                ]
            }
            result = model.extract_json(req.text, schema)
            raw_inferred = [
                EdgeInput(
                    subject=fact["subject"].lower().strip(),
                    object=fact["object"].lower().strip(),
                    rel_type=fact["rel_type"].lower().strip(),
                    subject_type=fact.get("subject_type"),
                    object_type=fact.get("object_type")
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

    facts, committed, staged = [], 0, 0
    if edges:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        try:
            gate, manager = WGMValidationGate(db, _rel_type_registry), FactStoreManager(db)
            registry = EntityRegistry(db)
            rows = []
            has_preferred = _detect_preference_signal(req.text)
            preferred_objects = set()
            # Map canonical UUID → original display name for alias sync (Bug #3 fix)
            _canonical_to_display: dict[str, str] = {}

            # The canonical user entity ID is the OpenWebUI UUID (req.user_id)
            user_entity_id = req.user_id

            # Load all aliases for this user's UUID
            _user_aliases = set()
            try:
                with db.cursor() as _cur:
                    _cur.execute(
                        "SELECT alias FROM entity_aliases WHERE user_id = %s AND entity_id = %s",
                        (req.user_id, user_entity_id),
                    )
                    _user_aliases.update(row[0] for row in _cur.fetchall())
                log.info("ingest.user_aliases_loaded",
                         count=len(_user_aliases), user_id=req.user_id)
            except Exception as _e:
                log.warning("ingest.user_aliases_load_failed", error=str(_e))

            for edge in edges:
                if edge.subject == edge.object: continue

                # Age fact validation: reject if subject="user" but no explicit self-statement
                if edge.rel_type.lower() == "age" and edge.subject.lower() == "user":
                    if "my" not in req.text.lower() or not any(pattern.search(req.text) for pattern in _IDENTITY_PATTERNS):
                        log.warning("ingest.age_rejected_wrong_subject",
                                    subject=edge.subject, object=edge.object, text=req.text[:100])
                        continue

                # UUID guard: reject raw edge values that are UUIDs
                # (canonical_ids may be UUIDs when entities exist without display names, which is fine)
                if _UUID_PATTERN.match(edge.subject) or _UUID_PATTERN.match(edge.object):
                    log.warning("ingest.uuid_value_rejected",
                                subject=edge.subject,
                                object=edge.object,
                                rel_type=edge.rel_type,
                                reason="raw UUID in edge subject or object — likely resolution leak")
                    continue

                # Capture raw scalar value before entity resolution
                _raw_object = edge.object

                # Rel_types that ALWAYS have scalar (string) objects, never UUID references
                _SCALAR_OBJECT_RELS = {
                    'pref_name', 'also_known_as',  # identity: display names (strings)
                    'age', 'height', 'weight', 'born_on',  # scalar attributes: measurements (strings)
                    'occupation', 'nationality',  # descriptive: single-word or phrase
                }

                # Resolve all entity names to canonical form via registry
                # This ensures aliases (mars, chris) never appear as subject/object in facts
                canonical_subject = registry.resolve(req.user_id, edge.subject)

                # For scalar rel_types, object is a string value (not an entity reference)
                # Skip resolution and keep the raw string value
                if edge.rel_type.lower() in _SCALAR_OBJECT_RELS:
                    canonical_object = edge.object.lower().strip()
                else:
                    # For relationship rel_types, resolve object to UUID
                    canonical_object = registry.resolve(req.user_id, edge.object)

                # Record display name mapping for alias sync (Bug #3 fix)
                # Only record for relationship facts where canonical_object is a UUID
                if edge.rel_type.lower() not in _SCALAR_OBJECT_RELS:
                    _canonical_to_display[canonical_object] = edge.object.lower()

                # Persist entity types to entities table if provided (only if currently unknown)
                if edge.subject_type and canonical_subject != user_entity_id:
                    try:
                        with db.cursor() as _cur:
                            _cur.execute(
                                "UPDATE entities SET entity_type = %s"
                                " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                (edge.subject_type.title(), canonical_subject, req.user_id),
                            )
                        db.commit()
                    except Exception as _e:
                        log.warning("ingest.subject_type_update_failed",
                                    entity_id=canonical_subject, entity_type=edge.subject_type, error=str(_e))

                # Only update entity types for relationship facts (not scalar values)
                if edge.object_type and edge.rel_type.lower() not in _SCALAR_OBJECT_RELS and canonical_object not in (user_entity_id, canonical_subject):
                    try:
                        with db.cursor() as _cur:
                            _cur.execute(
                                "UPDATE entities SET entity_type = %s"
                                " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                (edge.object_type.title(), canonical_object, req.user_id),
                            )
                        db.commit()
                    except Exception as _e:
                        log.warning("ingest.object_type_update_failed",
                                    entity_id=canonical_object, entity_type=edge.object_type, error=str(_e))

                # Relationship-aware type refinement: use relationship semantics to correct/override types
                # Example: has_pet(we, goose) → object should always be Animal, regardless of GLiNER2 classification
                rel_type_lower = edge.rel_type.lower()
                inferred_subject_type = _infer_type_from_relationship(rel_type_lower, "subject")
                inferred_object_type = _infer_type_from_relationship(rel_type_lower, "object")

                # Apply inferred types: prefer relationship semantics if provided (they're more reliable for validation)
                # If GLiNER2 type conflicts with relationship type, use relationship type
                final_subject_type = inferred_subject_type or edge.subject_type
                final_object_type = inferred_object_type or edge.object_type

                if final_subject_type and canonical_subject not in (user_entity_id, canonical_object):
                    try:
                        with db.cursor() as _cur:
                            _cur.execute(
                                "UPDATE entities SET entity_type = %s"
                                " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                (final_subject_type.title(), canonical_subject, req.user_id),
                            )
                        db.commit()
                    except Exception as _e:
                        log.warning("ingest.inferred_subject_type_update_failed",
                                    entity_id=canonical_subject, entity_type=final_subject_type, error=str(_e))

                if final_object_type and canonical_object not in (user_entity_id, canonical_subject):
                    try:
                        with db.cursor() as _cur:
                            _cur.execute(
                                "UPDATE entities SET entity_type = %s"
                                " WHERE id = %s AND user_id = %s AND entity_type = 'unknown'",
                                (final_object_type.title(), canonical_object, req.user_id),
                            )
                        db.commit()
                    except Exception as _e:
                        log.warning("ingest.inferred_object_type_update_failed",
                                    entity_id=canonical_object, entity_type=final_object_type, error=str(_e))

                # Extract and store pet descriptors for has_pet relationships
                # Example: "we have a cat named goose" → store species="cat" as entity_attribute
                if rel_type_lower == "has_pet" and canonical_object and canonical_object != user_entity_id:
                    descriptors = _extract_pet_descriptor(req.text, edge.object)
                    if descriptors:
                        try:
                            with db.cursor() as _cur:
                                for desc in descriptors:
                                    _cur.execute(
                                        "INSERT INTO entity_attributes (user_id, entity_id, attribute, value_text, sensitivity, provenance, category) "
                                        "VALUES (%s, %s, %s, %s, 'public', 'llm_inferred', 'physical') "
                                        "ON CONFLICT (user_id, entity_id, attribute) DO UPDATE SET "
                                        "value_text = EXCLUDED.value_text",
                                        (req.user_id, canonical_object, desc["type"], desc["value"]),
                                    )
                            db.commit()
                            log.info("ingest.pet_descriptor_stored",
                                     pet=canonical_object, descriptor_type=descriptors[0]["type"],
                                     descriptor_value=descriptors[0]["value"])
                        except Exception as _e:
                            log.warning("ingest.pet_descriptor_storage_failed",
                                       pet=canonical_object, error=str(_e))

                # Normalize user-identity aliases to the canonical user UUID
                if (canonical_subject.lower() in [a.lower() for a in _user_aliases] or canonical_subject == req.user_id) and canonical_subject != user_entity_id:
                    log.info("ingest.subject_normalized_to_user_id",
                             original=canonical_subject, user_id=user_entity_id,
                             matched_alias=canonical_subject.lower() in [a.lower() for a in _user_aliases])
                    canonical_subject = user_entity_id

                # Similarly for object, but only for rel_types where user can be an object.
                # Skip also_known_as and pref_name because those edges must preserve the alias as object.
                if (canonical_object in _user_aliases and canonical_object != user_entity_id and
                    edge.rel_type.lower() not in ("also_known_as", "pref_name")):
                    log.info("ingest.object_normalized_to_user_id",
                             original=canonical_object, user_id=user_entity_id)
                    canonical_object = user_entity_id

                # Track the actual subject to use for fact creation (may differ from canonical_subject
                # if this is a correction where subject resolved to user's identity)
                fact_subject = canonical_subject

                # Register aliases from also_known_as and pref_name edges
                if edge.rel_type.lower() in ("also_known_as", "pref_name"):
                    is_pref = (
                        edge.rel_type.lower() == "pref_name" or
                        edge.is_preferred_label or
                        edge.object.lower() in preferred_objects or
                        (has_preferred and edge.rel_type.lower() in ("also_known_as", "pref_name"))
                    )

                    # For corrections where subject is the user's canonical identity,
                    # find the entity we're actually aliasing (e.g., spouse, child)
                    alias_subject = canonical_subject
                    if (edge.is_correction and
                        alias_subject == registry.get_canonical_for_user(req.user_id)):
                        # Subject resolved to user identity. Look for related entities.
                        try:
                            with db.cursor() as _cur:
                                # Find most recent also_known_as/pref_name fact for related entity
                                _cur.execute(
                                    "SELECT subject_id FROM facts WHERE user_id = %s"
                                    " AND rel_type IN ('also_known_as', 'pref_name')"
                                    " AND subject_id != %s"
                                    " ORDER BY id DESC LIMIT 1",
                                    (req.user_id, alias_subject),
                                )
                                _row = _cur.fetchone()
                                if _row:
                                    alias_subject = _row[0]
                                    fact_subject = alias_subject  # Use resolved subject for fact creation
                                    log.info("ingest.correction_subject_resolved",
                                             original=canonical_subject, resolved=alias_subject,
                                             rel_type=edge.rel_type)
                        except Exception as _e:
                            log.warning("ingest.correction_subject_resolution_failed", error=str(_e))

                    registry.register_alias(
                        req.user_id,
                        alias_subject,
                        edge.object.lower(),
                        is_preferred=is_pref,
                    )
                    if is_pref and edge.rel_type.lower() == "pref_name":
                        preferred_objects.add(edge.object.lower())

                    # After a new user alias is registered, add it to in-memory set
                    # so subsequent edges in this batch are immediately normalized
                    if alias_subject == user_entity_id and edge.rel_type.lower() == "also_known_as":
                        _user_aliases.add(edge.object.lower())

                # Skip self-referential after resolution
                if fact_subject == canonical_object:
                    continue

                # Dynamically classify scalar vs relationship (replaces hardcoded _SCALAR_REL_TYPES)
                classification = classify_fact_type(
                    edge.rel_type.lower(), _raw_object.lower().strip(), registry, req.user_id)

                # Three-tier confidence logging (never reject, WGM gate enforces downstream)
                conf = classification.get("confidence", 0.5)
                if 0.60 <= conf < 0.80:
                    log.info("ingest.classifier_low_confidence",
                             rel_type=edge.rel_type.lower(), object=_raw_object,
                             type=classification["type"], confidence=conf, reason=classification["reason"])
                elif conf < 0.60:
                    log.warning("ingest.classifier_uncertain",
                                rel_type=edge.rel_type.lower(), object=_raw_object,
                                type=classification["type"], confidence=conf, reason=classification["reason"])

                if classification["type"] == "scalar":
                    val_text, val_int, val_float, val_date = _coerce_scalar(_raw_object.lower().strip())
                    # Only store if value is meaningful (reject non-numeric age etc.)
                    if edge.rel_type.lower() == "age" and val_int is None:
                        log.warning("ingest.scalar_rejected_non_numeric",
                                    entity=canonical_subject, value=canonical_object)
                        continue

                    # ROBUST: Determine the TRUE subject by looking at relationship context.
                    # If the current subject is the user but the text indicates this attribute belongs
                    # to a related entity (child, spouse, pet, etc.), resolve to that entity instead.
                    actual_subject = canonical_subject

                    is_user_subject = (actual_subject == user_entity_id or actual_subject == "user")

                    if is_user_subject and len(req.text.split()) > 3:
                        # Generic relation pattern: "my [relation] [name] is [value]"
                        # Works for any relation: son, daughter, wife, mother, dog, etc.
                        relation_pattern = r'\bmy\s+(\w+)\s+([A-Z][a-z]+)\s+(?:is|was|has|turned)\s+(\d+(?:\.\d+)?)\s*(?:years?\s+old|ft|lbs?|lb)?'
                        match = re.search(relation_pattern, req.text)

                        if match:
                            relation_type = match.group(1)  # son, daughter, wife, dog, etc.
                            entity_name = match.group(2).lower()  # Des, Sophia, Spot, etc.

                            # Resolve the mentioned entity to its canonical ID
                            resolved_entity = registry.resolve(req.user_id, entity_name)

                            # Verify this entity is actually related to the user via existing facts
                            if resolved_entity and resolved_entity != user_entity_id and resolved_entity != "user":
                                try:
                                    with db.cursor() as _verify_cur:
                                        _verify_cur.execute(
                                            "SELECT 1 FROM facts "
                                            "WHERE user_id = %s AND superseded_at IS NULL "
                                            "AND ((subject_id = %s AND object_id = %s) "
                                            "OR (subject_id = %s AND object_id = %s)) "
                                            "LIMIT 1",
                                            (req.user_id, user_entity_id, resolved_entity,
                                             resolved_entity, user_entity_id)
                                        )
                                        if _verify_cur.fetchone():
                                            actual_subject = resolved_entity
                                            log.info("ingest.scalar_redirected",
                                                     original=canonical_subject,
                                                     new=actual_subject,
                                                     relation=relation_type,
                                                     rel_type=edge.rel_type)
                                except Exception as _verify_e:
                                    log.warning("ingest.scalar_relation_verification_failed",
                                               error=str(_verify_e))

                    try:
                        # Ensure user entity exists in entities table
                        with db.cursor() as _cur:
                            _cur.execute(
                                "INSERT INTO entities (id, user_id, entity_type)"
                                " VALUES (%s, %s, %s)"
                                " ON CONFLICT (id, user_id) DO NOTHING",
                                (user_entity_id, req.user_id, "Person"),
                            )
                        db.commit()
                        _scalar_category = (
                            _REL_TYPE_META.get(edge.rel_type.lower(), {}).get("category")
                            or _infer_category(edge.rel_type.lower())
                        )
                        with db.cursor() as _cur:
                            _cur.execute(
                                "INSERT INTO entity_attributes"
                                " (user_id, entity_id, attribute, value_text, value_int,"
                                "  value_float, value_date, provenance, sensitivity, category)"
                                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                                " ON CONFLICT (user_id, entity_id, attribute)"
                                " DO UPDATE SET"
                                "   value_text = EXCLUDED.value_text,"
                                "   value_int = EXCLUDED.value_int,"
                                "   value_float = EXCLUDED.value_float,"
                                "   value_date = EXCLUDED.value_date,"
                                "   category = EXCLUDED.category,"
                                "   updated_at = now()",
                                (req.user_id, actual_subject, edge.rel_type.lower(),
                                 val_text, val_int, val_float, val_date, req.source,
                                 "private" if edge.rel_type.lower() in {
                                     "phone", "address", "email", "lives_at", "lives_in", "ip_address"
                                 } else "public", _scalar_category),
                            )
                        db.commit()
                        log.info("ingest.scalar_stored", entity=actual_subject, user_id=req.user_id,
                                 attribute=edge.rel_type, value_int=val_int, value_text=val_text,
                                 raw_input=_raw_object)
                    except Exception as _e:
                        log.warning("ingest.scalar_failed", error=str(_e))
                    continue  # Scalar facts stored in entity_attributes only, don't process as relationship

                # User corrections about themselves are axiomatically valid.
                # The gate exists to filter inferred/external data — not to override
                # explicit user intent. Bypass validation entirely for user self-corrections.
                if fact_subject == user_entity_id and edge.is_correction:
                    status = "valid"
                else:
                    validation = gate.validate_edge(
                        fact_subject, canonical_object, edge.rel_type,
                        user_id=req.user_id,
                        subject_type=edge.subject_type,
                        object_type=edge.object_type,
                    )
                    status = validation.get("status")

                    # Handle type_mismatch: user-stated facts override type constraints.
                    # The user is authoritative about their own data. Only reject
                    # type mismatches from pure inference (no user edges provided).
                    if status == "type_mismatch":
                        if req.edges:
                            # User-provided edges bypass type constraints — user is source of truth
                            status = "valid"
                            log.info("ingest.type_mismatch_overridden",
                                     subject=fact_subject,
                                     rel_type=edge.rel_type,
                                     object=canonical_object,
                                     reason="user-provided edges override type constraints")
                        else:
                            log.warning("ingest.type_mismatch",
                                        subject=fact_subject,
                                        rel_type=edge.rel_type,
                                        object=canonical_object,
                                        reason=validation.get("reason", ""))
                            continue

                # Look up whether this rel_type is engine_generated
                is_engine_generated = False
                if hasattr(_rel_type_registry, 'get') and _rel_type_registry:
                    rt_meta = _rel_type_registry.get(edge.rel_type.lower(), {})
                    is_engine_generated = rt_meta.get("engine_generated", False)

                edge_confidence = 1.0 if edge.is_correction else (
                    0.8 if edge.fact_provenance == "user_stated" else 0.6
                )

                fact_class = _classify_fact(
                    edge.rel_type,
                    edge_confidence,
                    engine_generated=is_engine_generated,
                    is_correction=edge.is_correction,
                )

                facts.append(FactResult(
                    subject=fact_subject,
                    object=canonical_object,
                    rel_type=edge.rel_type,
                    status=status,
                    fact_class=fact_class,
                    provenance=edge.fact_provenance,
                ))
                if status in ("valid", "conflict"):
                    # Conflict facts: the WGM gate already inserted the new fact and marked
                    # old facts as contradicted. We still need rows populated for downstream
                    # processing (entity alias sync, Qdrant sync, preference propagation).
                    # pref_name edges are always preferred by definition — the rel_type itself
                    # is the preference signal. also_known_as requires explicit signal to be preferred.
                    if edge.rel_type.lower() == "pref_name":
                        is_preferred = True
                    else:
                        is_preferred = (
                            edge.rel_type.lower() == "also_known_as" and
                            (has_preferred or edge.is_preferred_label or edge.is_correction)
                        )
                    rows.append((
                        req.user_id, fact_subject, canonical_object,
                        edge.rel_type, req.source, is_preferred,
                        fact_class, edge_confidence, is_engine_generated
                    ))

            if rows:
                # Rel_types that ALWAYS have scalar (string) objects, never UUID references
                _SCALAR_OBJECT_RELS = {
                    'pref_name', 'also_known_as',  # identity: display names (strings)
                    'age', 'height', 'weight', 'born_on',  # scalar attributes: measurements (strings)
                    'occupation', 'nationality',  # descriptive: single-word or phrase
                }

                # Filter rows to ensure entity_ids are valid (UUIDs or user_id itself)
                # Validates that only user_id and UUID v5 surrogates appear in facts
                # (prevents arbitrary string entity_ids from contaminating DB)
                validated_rows = []
                for row in rows:
                    user_id, subject, obj, rel_type, source, is_preferred, fact_class, confidence, is_engine_generated = row
                    rt_lower = rel_type.lower() if rel_type else ''

                    # If subject is a string (not UUID), try to resolve it
                    if subject and not _UUID_PATTERN.match(subject) and subject != user_id:
                        try:
                            subject = registry.resolve(user_id, subject)
                            log.info("ingest.subject_resolved_during_validation",
                                   original=row[1], resolved=subject, rel_type=rel_type)
                        except Exception as _e:
                            log.error("ingest.subject_resolution_failed_validation",
                                    original=row[1], error=str(_e), rel_type=rel_type)
                            continue

                    # If object is a string, only resolve if rel_type expects UUID objects
                    # Skip resolution for rel_types in _SCALAR_OBJECT_RELS (pref_name, age, etc.)
                    if obj and not _UUID_PATTERN.match(obj) and obj != user_id:
                        if rt_lower not in _SCALAR_OBJECT_RELS:
                            try:
                                obj = registry.resolve(user_id, obj)
                                log.info("ingest.object_resolved_during_validation",
                                       original=row[2], resolved=obj, rel_type=rel_type)
                            except Exception as _e:
                                log.error("ingest.object_resolution_failed_validation",
                                        original=row[2], error=str(_e), rel_type=rel_type)
                                continue

                    # Now validate: subject must be UUID or user_id
                    is_valid_subject = (
                        subject == user_id or
                        _UUID_PATTERN.match(subject)
                    )
                    if not is_valid_subject:
                        log.error("ingest.invalid_subject_id",
                                  subject=subject, rel_type=rel_type, user_id=user_id,
                                  reason="subject_id must be UUID or user_id")
                        continue  # Skip this fact

                    # Object validation depends on rel_type:
                    # - For scalar rel_types: object can be ANY string (value)
                    # - For relationship rel_types: object must be UUID or user_id
                    if rt_lower in _SCALAR_OBJECT_RELS:
                        # Scalar rel_type: object can be any non-empty string
                        if not obj or not obj.strip():
                            log.error("ingest.invalid_object_scalar",
                                      obj=obj, rel_type=rel_type, user_id=user_id,
                                      reason="object value cannot be empty for scalar rel_type")
                            continue
                    else:
                        # Relationship rel_type: object must be UUID or user_id
                        is_valid_object = (
                            obj == user_id or
                            _UUID_PATTERN.match(obj)
                        )
                        if not is_valid_object:
                            log.error("ingest.invalid_object_id",
                                      obj=obj, rel_type=rel_type, user_id=user_id,
                                      reason="object_id must be UUID or user_id for relationship rel_type")
                            continue  # Skip this fact

                    # Update row with resolved subject/object if they changed
                    updated_row = (user_id, subject, obj, rel_type, source, is_preferred, fact_class, confidence, is_engine_generated)
                    validated_rows.append(updated_row)

                rows = validated_rows

                # Split rows by fact class — surrogates go directly to commit, no display name resolution
                # Display names are resolved at READ time only (_resolve_display_names in /query)
                class_a_rows = []
                class_b_rows = []
                class_c_rows = []

                for user_id, subject, obj, rel_type, source, is_preferred, fact_class, _, is_engine_generated in rows:
                    if fact_class == "A":
                        class_a_rows.append((user_id, subject, obj, rel_type, source, is_preferred))
                    elif fact_class == "B":
                        class_b_rows.append((user_id, subject, obj, rel_type, source))
                    else:
                        class_c_rows.append((user_id, subject, obj, rel_type, source))

                committed = 0
                staged = 0
                if class_a_rows:
                    committed += manager.commit(class_a_rows)
                    log.info("ingest.class_a_committed", count=len(class_a_rows))

                    # Trigger immediate Qdrant sync for Class A facts (don't wait for 10s re_embedder poll)
                    # This ensures attribute queries immediately after ingest get results from both PostgreSQL and Qdrant
                    try:
                        qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
                        qwen_api_url = os.environ.get("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
                        upserted = 0
                        with db.cursor() as cur:
                            # Fetch the facts we just committed (qdrant_synced=false)
                            cur.execute(
                                "SELECT id, user_id, subject_id, object_id, rel_type FROM facts "
                                "WHERE user_id = %s AND qdrant_synced = false AND superseded_at IS NULL "
                                "ORDER BY id DESC LIMIT %s",
                                (req.user_id, len(class_a_rows))
                            )
                            fresh_facts = cur.fetchall()
                            if fresh_facts:
                                collection = derive_collection(req.user_id)
                                ensure_collection(collection, qdrant_url)
                                for fact_id, fact_user_id, subject, obj, rel_type in fresh_facts:
                                    text = f"{subject} {rel_type} {obj}"
                                    vector = embed_text(text, qwen_api_url, timeout=10.0, fallback=True)
                                    if vector is None:
                                        continue
                                    row = {
                                        "id": fact_id,
                                        "user_id": fact_user_id,
                                        "subject_id": subject,
                                        "subject_display": subject,
                                        "object_id": obj,
                                        "object_display": obj,
                                        "rel_type": rel_type,
                                        "provenance": req.source,
                                        "confidence": 1.0,
                                        "confirmed_count": 0,
                                        "last_seen_at": None,
                                        "contradicted_by": None,
                                    }
                                    if upsert_to_qdrant(row, vector, collection, qdrant_url):
                                        mark_synced(db, fact_id)
                                        upserted += 1
                                if upserted > 0:
                                    db.commit()
                                    log.info("ingest.immediate_qdrant_sync", user_id=req.user_id, synced=upserted)
                    except Exception as _sync_err:
                        log.warning("ingest.immediate_qdrant_sync_failed", error=str(_sync_err))
                        # Don't fail ingest if immediate Qdrant sync fails — it will retry on next re_embedder poll

                if class_b_rows:
                    staged_b = _commit_staged(db, class_b_rows, "B", confidence=0.8)
                    staged += staged_b
                    log.info("ingest.class_b_staged", count=staged_b)

                    # Trigger immediate Qdrant sync for Class B staged facts
                    # so they're available via vector search without waiting for re_embedder poll
                    try:
                        qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
                        qwen_api_url = os.environ.get("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
                        upserted_b = 0
                        with db.cursor() as cur:
                            cur.execute(
                                "SELECT id, user_id, subject_id, object_id, rel_type, provenance, confidence FROM staged_facts "
                                "WHERE user_id = %s AND qdrant_synced = false AND promoted_at IS NULL AND expires_at > now() "
                                "ORDER BY id DESC LIMIT %s",
                                (req.user_id, len(class_b_rows))
                            )
                            staged_fresh = cur.fetchall()
                            if staged_fresh:
                                collection = derive_collection(req.user_id)
                                ensure_collection(collection, qdrant_url)
                                for sf_id, sf_uid, sf_subj, sf_obj, sf_rel, sf_prov, sf_conf in staged_fresh:
                                    text = f"{sf_subj} {sf_rel} {sf_obj}"
                                    vector = embed_text(text, qwen_api_url, timeout=10.0, fallback=True)
                                    if vector is None:
                                        continue
                                    s_row = {
                                        "id": sf_id,
                                        "user_id": sf_uid,
                                        "subject_id": sf_subj,
                                        "subject_display": sf_subj,
                                        "object_id": sf_obj,
                                        "object_display": sf_obj,
                                        "rel_type": sf_rel,
                                        "provenance": sf_prov,
                                        "confidence": float(sf_conf) if sf_conf else 0.8,
                                        "confirmed_count": 0,
                                        "last_seen_at": None,
                                        "contradicted_by": None,
                                    }
                                    if upsert_to_qdrant(s_row, vector, collection, qdrant_url):
                                        with db.cursor() as _mark:
                                            _mark.execute(
                                                "UPDATE staged_facts SET qdrant_synced = true WHERE id = %s",
                                                (sf_id,)
                                            )
                                        db.commit()
                                        upserted_b += 1
                                if upserted_b > 0:
                                    log.info("ingest.immediate_qdrant_sync_staged", user_id=req.user_id, synced=upserted_b)
                    except Exception as _sync_err:
                        log.warning("ingest.immediate_qdrant_sync_staged_failed", error=str(_sync_err))

                if class_c_rows:
                    staged_c = _commit_staged(db, class_c_rows, "C", confidence=0.4)
                    staged += staged_c
                    log.info("ingest.class_c_staged", count=staged_c)

                # Use class_a_rows for downstream corrections processing
                resolved_rows = class_a_rows

                # Build a map of edges to identify which rows are corrections
                # Key is (original_subject, object, rel_type); value is whether it's a correction
                correction_map = {}
                for edge in edges:
                    key = (edge.subject.lower(), edge.object.lower(), edge.rel_type.lower())
                    correction_map[key] = edge.is_correction

                # Build set of preferred objects from pref_name rows in this batch
                # e.g. christopher → chris → pref_name means "chris" is preferred
                batch_preferred_objects = {
                    obj.lower() for _, subject, obj, rel_type, _, is_preferred in resolved_rows
                    if rel_type.lower() == "pref_name" and is_preferred
                }

                with db.cursor() as cur:
                    for row in resolved_rows:
                        user_id, subject, obj, rel_type, source, is_preferred = row
                        if rel_type.lower() == "also_known_as" and is_preferred:
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = false"
                                " WHERE user_id = %s AND subject_id = %s AND rel_type = 'also_known_as'"
                                " AND object_id != %s",
                                (user_id, subject, obj),
                            )

                    # Propagate preferred label from pref_name to matching user → also_known_as rows
                    if batch_preferred_objects:
                        for preferred_obj in batch_preferred_objects:
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = true"
                                " WHERE user_id = %s AND subject_id = 'user'"
                                " AND rel_type = 'also_known_as' AND object_id = %s",
                                (req.user_id, preferred_obj),
                            )
                            # Clear other user → also_known_as preferred labels
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = false"
                                " WHERE user_id = %s AND subject_id = 'user'"
                                " AND rel_type = 'also_known_as' AND object_id != %s",
                                (req.user_id, preferred_obj),
                            )

                    # Sync is_preferred to entity_aliases after every also_known_as / pref_name commit.
                    # This is the authoritative preference flip — entity_aliases drives query-time
                    # identity resolution. facts.is_preferred_label is secondary.
                    log.info("ingest.sync_debug",
                             resolved_rows=[(r[1], r[2], r[3], r[5]) for r in resolved_rows])
                    for row in resolved_rows:
                        _uid, _subj, _obj, _rel, _src, _is_pref = row
                        if _rel.lower() not in ("also_known_as", "pref_name"):
                            continue

                        # Resolve display name from canonical UUID (Bug #3 fix)
                        _display_name = _canonical_to_display.get(_obj, _obj)

                        # Upsert alias into entity_aliases
                        cur.execute(
                            "INSERT INTO entity_aliases (entity_id, user_id, alias, is_preferred) "
                            "VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (user_id, alias) "
                            "DO UPDATE SET is_preferred = EXCLUDED.is_preferred",
                            (_subj, _uid, _display_name, _is_pref),
                        )

                        # If this is a hard preference, demote all other aliases for this entity
                        if _is_pref:
                            cur.execute(
                                "UPDATE entity_aliases SET is_preferred = false "
                                "WHERE user_id = %s AND entity_id = %s AND alias != %s",
                                (_uid, _subj, _display_name),
                            )
                            # Mirror into facts: demote other also_known_as rows for this entity
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = false, qdrant_synced = false "
                                "WHERE user_id = %s AND subject_id = %s "
                                "AND rel_type IN ('also_known_as', 'pref_name') "
                                "AND object_id != %s AND superseded_at IS NULL "
                                "AND hard_delete_flag = false",
                                (_uid, _subj, _obj),
                            )
                            log.info("ingest.preferred_name_flipped",
                                     entity=_subj, new_preferred=_obj, user_id=_uid)

                    for row in resolved_rows:
                        user_id, subject, obj, rel_type, source, is_preferred = row

                        # Check if this row came from a correction edge
                        # Match by object and rel_type (subject may have been resolved)
                        is_correction = any(
                            e.is_correction and
                            e.object.lower() == obj.lower() and
                            e.rel_type.lower() == rel_type.lower()
                            for e in edges
                        )
                        if not is_correction:
                            continue

                        # For also_known_as/pref_name corrections where subject is user's canonical identity,
                        # find the actual entity being corrected (e.g., wife entity when user said "my wife...")
                        correction_subject = subject.lower()
                        correction_object = obj.lower()

                        if rel_type.lower() in ("also_known_as", "pref_name"):
                            canonical_user = registry.get_canonical_for_user(user_id)
                            if canonical_user and correction_subject == canonical_user:
                                # Subject is the user's canonical ID. Find the entity we're actually correcting
                                # by looking for the most recent also_known_as/pref_name fact for a related entity
                                cur.execute(
                                    "SELECT subject_id FROM facts WHERE user_id = %s"
                                    " AND rel_type IN ('also_known_as', 'pref_name')"
                                    " AND subject_id != %s"
                                    " ORDER BY id DESC LIMIT 1",
                                    (user_id, correction_subject),
                                )
                                candidate = cur.fetchone()
                                if candidate:
                                    correction_subject = candidate[0]
                                    log.info("correction.subject_resolved",
                                             original=subject, resolved=correction_subject,
                                             rel_type=rel_type)

                        # Identity rels (also_known_as, pref_name, same_as): lookup by object (the NAME being corrected)
                        # All other rels: lookup by subject + rel_type (the FACT being corrected, regardless of new value)
                        # This allows age→10, location→Paris, occupation→Engineer corrections to supersede old values
                        _IDENTITY_RELS = {"also_known_as", "pref_name", "same_as"}

                        if rel_type.lower() in _IDENTITY_RELS:
                            # Identity: correct the name (object is the name)
                            cur.execute(
                                "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                                " AND object_id = %s AND rel_type = %s",
                                (user_id, correction_subject, correction_object, rel_type.lower()),
                            )
                        else:
                            # Non-identity: correct the fact (find most recent by subject + rel_type)
                            cur.execute(
                                "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                                " AND rel_type = %s ORDER BY id DESC LIMIT 1",
                                (user_id, correction_subject, rel_type.lower()),
                            )
                        result = cur.fetchone()

                        # Fallback for identity rels: if fact not found by object, try by subject
                        # (fact might have been inserted with wrong subject due to resolution)
                        if not result and rel_type.lower() in _IDENTITY_RELS:
                            cur.execute(
                                "SELECT id FROM facts WHERE user_id = %s AND subject_id = %s"
                                " AND object_id = %s AND rel_type = %s",
                                (user_id, subject.lower(), correction_object, rel_type.lower()),
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
                        _apply_correction(cur, user_id, correction_subject, correction_object,
                                          rel_type.lower(), new_fact_id, behavior)
                        if rel_type.lower() == "also_known_as":
                            cur.execute(
                                "UPDATE facts SET is_preferred_label = true WHERE id = %s",
                                (new_fact_id,),
                            )

                    db.commit()
        finally: db.close()

    return IngestResponse(status="valid", committed=committed, staged=staged,
                          entities=[EntityResult(entity=r["entity"], label=r["type"], canonical_id=r["canonical_id"]) for r in resolved],
                          facts=facts)

@app.post("/query")
def query(request: QueryRequest):
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    qwen_api_url = os.environ.get("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")
    user_id = request.user_id or "anonymous"
    collection = derive_collection(user_id)

    preferred_names = {}
    canonical_identity = None
    baseline_facts = []
    attributes = {}
    resolved_entity_ids = set()  # Track entities resolved in entity_resolution block
    user_surrogate = user_id  # OWUI UUID IS the surrogate — always
    db = None
    registry = None
    def _fetch_user_facts(
        db_conn,
        user_id: str,
        entity_id: str = None,
        rel_types: tuple[str, ...] = None,
    ) -> list[dict]:
        """
        Fetch facts from BOTH facts and staged_facts tables.
        Uses two separate queries (not UNION) to avoid parameter binding
        issues with psycopg2 shared params across multiple SELECTs.
        Attaches fact_state metadata so the Filter can distinguish
        long-term (authoritative) from staged (provisional) facts.
        Returns list of {subject, object, rel_type, provenance, confidence,
                          category, fact_state, fact_class,
                          staged_confirmations, promoted_at, expires_at} dicts.
        """
        results = []
        try:
            with db_conn.cursor() as cur:
                # --- Build base conditions ---
                f_conds = ["user_id = %s", "superseded_at IS NULL",
                           "hard_delete_flag = false",
                           "(valid_until IS NULL OR valid_until > now())"]
                s_conds = ["user_id = %s", "expires_at > now()",
                           "promoted_at IS NULL"]

                if rel_types:
                    ph = ",".join(["%s"] * len(rel_types))
                    f_conds.append(f"rel_type IN ({ph})")
                    s_conds.append(f"rel_type IN ({ph})")

                if entity_id:
                    f_conds.append("(subject_id = %s OR object_id = %s)")
                    s_conds.append("(subject_id = %s OR object_id = %s)")

                f_where = " AND ".join(f_conds)
                s_where = " AND ".join(s_conds)

                # --- Build independent params for each query ---
                base_params = [user_id]
                f_params = list(base_params)
                s_params = list(base_params)

                if rel_types:
                    f_params.extend(rel_types)
                    s_params.extend(rel_types)

                if entity_id:
                    f_params.extend([entity_id, entity_id])
                    s_params.extend([entity_id, entity_id])

                # --- Query 1: facts table (long-term) ---
                f_query = (
                    f"SELECT subject_id, object_id, rel_type, provenance, confidence,"
                    f"  confirmed_count, fact_class FROM facts "
                    f"WHERE {f_where}"
                )
                cur.execute(f_query, f_params)
                seen = set()
                for r in cur.fetchall():
                    key = (r[0], r[1], r[2])
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            "subject": r[0], "object": r[1], "rel_type": r[2],
                            "provenance": r[3],
                            "confidence": float(r[4]) if r[4] else 1.0,
                            "category": _REL_TYPE_META.get(r[2], {}).get("category") or _infer_category(r[2]),
                            "fact_state": "long_term",
                            "fact_class": r[6] if r[6] else "A",
                            "staged_confirmations": r[5] if r[5] else 0,
                            "promoted_at": None,
                            "expires_at": None,
                        })

                # --- Query 2: staged_facts table (provisional) ---
                s_query = (
                    f"SELECT subject_id, object_id, rel_type, provenance, confidence,"
                    f"  confirmed_count, fact_class, promoted_at, expires_at FROM staged_facts "
                    f"WHERE {s_where}"
                )
                cur.execute(s_query, s_params)
                for r in cur.fetchall():
                    key = (r[0], r[1], r[2])
                    if key not in seen:
                        seen.add(key)
                        promoted = r[7].isoformat() if r[7] else None
                        expires = r[8].isoformat() if r[8] else None
                        results.append({
                            "subject": r[0], "object": r[1], "rel_type": r[2],
                            "provenance": r[3],
                            "confidence": float(r[4]) if r[4] else 0.0,
                            "category": _REL_TYPE_META.get(r[2], {}).get("category") or _infer_category(r[2]),
                            "fact_state": "staged",
                            "fact_class": r[6] if r[6] else "B",
                            "staged_confirmations": r[5] if r[5] else 0,
                            "promoted_at": promoted,
                            "expires_at": expires,
                        })
        except Exception as e:
            log.warning("query.fetch_user_facts_failed", error=str(e))
        return results
    def _fetch_attributes(
        db_conn,
        user_id: str,
        entity_ids: list[str],
        max_sensitivity: str = "private",
    ) -> dict:
        """
        Fetch entity_attributes for a list of entity IDs.
        max_sensitivity controls which tiers are returned:
          'public'  — only public attributes
          'private' — public + private (default)
          'secret'  — all including secret (never use in query path)
        Returns {entity_id: {attribute: value}} dict.
        """
        if not entity_ids:
            return {}
        sensitivity_filter = {
            "public": ("public",),
            "private": ("public", "private"),
            "secret": ("public", "private", "secret"),
        }.get(max_sensitivity, ("public", "private"))

        try:
            _ph = ",".join(["%s"] * len(entity_ids))
            _sh = ",".join(["%s"] * len(sensitivity_filter))
            with db_conn.cursor() as cur:
                cur.execute(
                    f"SELECT entity_id, attribute, value_int, value_float, value_text, "
                    f"value_date, category "
                    f"FROM entity_attributes "
                    f"WHERE user_id = %s AND entity_id IN ({_ph}) "
                    f"AND sensitivity IN ({_sh})",
                    [user_id] + list(entity_ids) + list(sensitivity_filter),
                )
                attributes = {}
                for row in cur.fetchall():
                    eid, attr, vi, vf, vt, vd, cat = row
                    if eid not in attributes:
                        attributes[eid] = {}
                    attributes[eid][attr] = {
                        "value": (
                            vi if vi is not None else
                            vf if vf is not None else
                            vt if vt is not None else
                            str(vd) if vd is not None else None
                        ),
                        "category": cat,
                    }
                return attributes
        except Exception as e:
            log.warning("query.attributes_fetch_failed", error=str(e))
            return {}

    def _resolve_display_names(facts: list[dict], registry, user_id: str, user_entity_id: str = "user") -> list[dict]:
        """
        Resolve UUID subject_id/object_id to preferred display names.
        Falls back to the UUID string if no alias found.
        """
        resolved = []
        for f in facts:
            subject_display = registry.get_preferred_name(user_id, f["subject"])
            object_display = registry.get_preferred_name(user_id, f["object"])
            
            # FIXED: Normalize user entity IDs to "user" placeholder for consistent display
            if f["subject"] == user_entity_id or subject_display == user_entity_id:
                subject_display = "user"
            if f["object"] == user_entity_id or object_display == user_entity_id:
                object_display = "user"
            
            resolved.append({
                **f,
                "subject": subject_display,
                "object": object_display,
            })
        return resolved

    def _attributes_to_facts(attributes: dict) -> list[dict]:
        """
        Convert entity_attributes to facts format for injection.
        Each attribute becomes a fact with subject=entity, rel_type=attribute.
        Works for all entities, not just "user".
        """
        facts = []
        for entity_id, entity_attrs in attributes.items():
            if not isinstance(entity_attrs, dict):
                continue
            for attribute, attr_data in entity_attrs.items():
                if isinstance(attr_data, dict):
                    value = attr_data.get("value")
                    category = attr_data.get("category")
                else:
                    # Backward compat: legacy scalar values
                    value = attr_data
                    category = None
                if value is not None:
                    facts.append({
                        "subject": entity_id,
                        "rel_type": attribute,
                        "object": str(value),
                        "confidence": 1.0,
                        "fact_class": "A",
                        "fact_state": "long_term",
                        "category": category,
                    })
        return facts

    # Graph traversal path: if query contains self-referential signals,
    # fetch facts directly from Postgres anchored to the user's identity.
    # This bypasses vector similarity which fails for structured relational queries.
    _SELF_REF_SIGNALS = {
        "my family", "my children", "my kids", "my wife", "my husband",
        "my spouse", "my partner", "my parents", "my siblings", "my brother",
        "my sister", "my son", "my daughter", "about me", "about myself",
        "who am i", "who i am", "list my", "tell me about me",
        "what do you know about me", "my pets", "my animals", "my home",
        "where do i live", "my address", "my age", "my job", "my work",
    }

    # The canonical user entity ID is the OpenWebUI UUID
    user_entity_id_for_query = user_id

    # Initialize database connection and entity registry
    try:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        registry = EntityRegistry(db)
        # Canonical identity is the user's preferred display name for themselves
        canonical_identity = registry.get_preferred_name(user_id, user_id)
    except Exception as _e:
        log.warning("query.db_init_failed", error=str(_e))
        db = None
        registry = None
        canonical_identity = None

    query_lower = request.text.lower()
    direct_facts = []
    if any(signal in query_lower for signal in _SELF_REF_SIGNALS):
        try:
            if db and registry and canonical_identity:
                direct_facts = _fetch_user_facts(db, user_id, entity_id=user_entity_id_for_query)

                # 2-hop: fetch facts for directly related entities
                related = {
                    f["object"] for f in direct_facts if f["subject"] == user_entity_id_for_query
                } | {
                    f["subject"] for f in direct_facts if f["object"] == user_entity_id_for_query
                }
                related.discard(user_entity_id_for_query)

                if related:
                    with db.cursor() as _cur:
                        _rel_list = list(related)
                        _rel_ph = ",".join(["%s"] * len(_rel_list))
                        _cur.execute(
                            f"SELECT subject_id, object_id, rel_type, provenance, confidence FROM facts "
                            f"WHERE user_id = %s AND superseded_at IS NULL "
                            f"AND hard_delete_flag = false "
                            f"AND (valid_until IS NULL OR valid_until > now()) "
                            f"AND (subject_id IN ({_rel_ph}) OR object_id IN ({_rel_ph})) "
                            f"ORDER BY id",
                            [user_id] + _rel_list + _rel_list,
                        )
                        seen = {(f["subject"], f["object"], f["rel_type"]) for f in direct_facts}
                        for row in _cur.fetchall():
                            key = (row[0], row[1], row[2])
                            if key not in seen:
                                direct_facts.append({
                                    "subject": row[0], "object": row[1],
                                    "rel_type": row[2], "provenance": row[3],
                                    "confidence": float(row[4]) if row[4] else 1.0,
                                    "category": _REL_TYPE_META.get(row[2], {}).get("category") or _infer_category(row[2]),
                                    "fact_state": "long_term",
                                    "fact_class": "A",
                                })
                                seen.add(key)

                entity_ids = list({f["subject"] for f in direct_facts} | {f["object"] for f in direct_facts})
                attributes = _fetch_attributes(db, user_id, entity_ids, max_sensitivity="private")
                if direct_facts:
                    log.info("query.graph_traversal", identity=canonical_identity, hits=len(direct_facts))
                    # Don't return early — merge with Qdrant results below
                    # Postgres facts are authoritative; Qdrant adds associative context
        except Exception as _e:
            log.warning("query.graph_traversal_failed", error=str(_e))
        finally:
            pass

    # Named-entity attribute resolution
    # Triggered when query contains attribute signals
    # # NO RECURSIVE MATCHING — all comparisons use pre-lowercased query_lower only
    _ATTRIBUTE_SIGNALS = {
        "old", "age", "height", "tall", "weight", "heavy",
        "job", "work", "occupation", "born", "birthday",
        "live", "address", "home", "location",
    }

    _STOPWORDS = {
        "how", "what", "is", "are", "was", "the", "a", "an",
        "my", "your", "his", "her", "their", "our", "its",
        "do", "does", "did", "please", "tell", "me", "about"
    }

    has_attribute_signal = any(sig in query_lower for sig in _ATTRIBUTE_SIGNALS)
    log.info("query.entity_resolution.start", has_attribute_signal=has_attribute_signal, query_lower=query_lower)

    if has_attribute_signal and db:
        try:
            # Tokenize query, strip stopwords, get candidate name tokens
            tokens = [
                t.strip("?.,!").lower()
                for t in request.text.split()
                if t.strip("?.,!").lower() not in _STOPWORDS
                and len(t.strip("?.,!")) > 1
            ]
            log.info("query.entity_resolution.tokens", tokens=tokens)

            if tokens:
                # Resolve tokens against entity_aliases for this user
                placeholders = ",".join(["%s"] * len(tokens))
                with db.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT DISTINCT ea.entity_id, ea.alias
                        FROM entity_aliases ea
                        WHERE ea.user_id = %s
                          AND lower(ea.alias) IN ({placeholders})
                        """,
                        [user_id] + tokens
                    )
                    resolved = cur.fetchall()
                log.info("query.entity_resolution.resolved", resolved=[(r[0], r[1]) for r in resolved])

                if resolved:
                    resolved_ids = {row[0] for row in resolved}

                    # Verify each resolved entity is related to the user
                    # (appears as subject or object in an existing fact)
                    with db.cursor() as cur:
                        id_placeholders = ",".join(["%s"] * len(resolved_ids))
                        cur.execute(
                            f"""
                            SELECT DISTINCT subject_id, object_id
                            FROM facts
                            WHERE user_id = %s
                              AND superseded_at IS NULL
                              AND hard_delete_flag = false
                              AND (subject_id IN ({id_placeholders})
                                   OR object_id IN ({id_placeholders}))
                            """,
                            [user_id] + list(resolved_ids) + list(resolved_ids)
                        )
                        related_rows = cur.fetchall()

                    # Collect confirmed related entity IDs
                    confirmed_ids = set()
                    for row in related_rows:
                        if row[0] in resolved_ids:
                            confirmed_ids.add(row[0])
                        if row[1] in resolved_ids:
                            confirmed_ids.add(row[1])
                    log.info("query.entity_resolution.confirmed", confirmed_ids=list(confirmed_ids))
                    # Track resolved entities so we fetch their attributes later
                    resolved_entity_ids.update(confirmed_ids)

                    if confirmed_ids:
                        # Fetch facts anchored to confirmed entities
                        with db.cursor() as cur:
                            id_placeholders = ",".join(["%s"] * len(confirmed_ids))
                            cur.execute(
                                f"""
                                SELECT subject_id, object_id, rel_type, provenance, confidence
                                FROM facts
                                WHERE user_id = %s
                                  AND superseded_at IS NULL
                                  AND hard_delete_flag = false
                                  AND (subject_id IN ({id_placeholders})
                                       OR object_id IN ({id_placeholders}))
                                """,
                                [user_id] + list(confirmed_ids) + list(confirmed_ids)
                            )
                            entity_facts = cur.fetchall()

                        # Fetch attributes for confirmed entities
                        entity_attrs = _fetch_attributes(
                            db, user_id, list(confirmed_ids),
                            max_sensitivity="private"
                        )
                        log.info("query.entity_resolution.attributes", entity_attrs=entity_attrs)

                        # Explicitly fetch scalar facts for resolved entities
                        # (age, height, weight, born_on) to ensure they appear in facts array
                        scalar_facts = []
                        with db.cursor() as cur:
                            id_placeholders = ",".join(["%s"] * len(confirmed_ids))
                            cur.execute(
                                f"""
                                SELECT subject_id, object_id, rel_type, provenance, confidence
                                FROM facts
                                WHERE user_id = %s
                                  AND superseded_at IS NULL
                                  AND hard_delete_flag = false
                                  AND rel_type IN ('age', 'height', 'weight', 'born_on')
                                  AND subject_id IN ({id_placeholders})
                                """,
                                [user_id] + list(confirmed_ids)
                            )
                            scalar_facts = [
                                {
                                    "subject": row[0],
                                    "object": row[1],
                                    "rel_type": row[2],
                                    "provenance": row[3],
                                    "confidence": float(row[4]) if row[4] else 1.0,
                                    "category": "physical"
                                }
                                for row in cur.fetchall()
                            ]
                        log.info("query.entity_resolution.scalar_facts", count=len(scalar_facts))

                        # Merge into direct_facts and attributes
                        for row in entity_facts:
                            fact = {
                                "subject": row[0],
                                "object": row[1],
                                "rel_type": row[2],
                                "provenance": row[3],
                                "confidence": float(row[4]) if row[4] else 0.0,
                                "category": _REL_TYPE_META.get(row[2], {}).get("category")
                                            or _infer_category(row[2])
                            }
                            if fact not in direct_facts:
                                direct_facts.append(fact)

                        # Merge scalar facts into direct_facts (avoid duplicates)
                        existing_keys = {(f["subject"], f["object"], f["rel_type"]) for f in direct_facts}
                        for sf in scalar_facts:
                            key = (sf["subject"], sf["object"], sf["rel_type"])
                            if key not in existing_keys:
                                direct_facts.append(sf)
                                existing_keys.add(key)

                        for entity_id, attr_dict in entity_attrs.items():
                            if entity_id not in attributes:
                                attributes[entity_id] = attr_dict
                            else:
                                attributes[entity_id].update(attr_dict)
        except Exception as e:
            log.error("query.entity_resolution.error", error=str(e))

    # Embed after graph traversal so Postgres results are returned even when the
    # embedding service is unavailable. fallback=False: skip Qdrant rather than
    # searching with a hash vector that can't match nomic-embedded stored facts.
    vector = embed_text(request.text, qwen_api_url, timeout=10.0, fallback=False)
    if vector is None:
        log.warning("query.embed_unavailable — skipping Qdrant search")
        # Resolve display names for Postgres facts before returning
        resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
        resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
        try:
            _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
            _early_ids = list({user_entity_id_for_query} | {f["subject"] for f in resolved_direct + resolved_baseline} | {f["object"] for f in resolved_direct + resolved_baseline} | resolved_entity_ids)
            attributes = _fetch_attributes(_attr_db, user_id, _early_ids, max_sensitivity="private")
            _attr_db.close()
        except Exception:
            pass
        attr_facts = _attributes_to_facts(attributes)
        log.info("query.embed_fail_attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
        resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
        merged_facts = resolved_direct + resolved_baseline + resolved_attr_facts
        log.info("query.embed_fail_merged", attr_count=len(resolved_attr_facts), total=len(merged_facts))
        # Populate preferred_names with all entities in merged facts
        if registry:
            for f in merged_facts:
                if f["subject"] not in preferred_names and f["subject"] != user_entity_id_for_query:
                    preferred_names[f["subject"]] = registry.get_preferred_name(user_id, f["subject"]) or f["subject"].title()
                if f["object"] not in preferred_names and f["object"] != user_entity_id_for_query:
                    preferred_names[f["object"]] = registry.get_preferred_name(user_id, f["object"]) or f["object"].title()
        return {
            "status": "ok",
            "facts": merged_facts,
            "preferred_names": preferred_names,
            "canonical_identity": canonical_identity,
            "attributes": attributes,
        }

    try:
        resp = httpx.post(
            f"{qdrant_url}/collections/{collection}/points/search",
            json={"vector": vector, "limit": 10, "with_payload": True, "score_threshold": 0.3},
            timeout=10.0,
        )
        if resp.status_code == 404:
            ensure_collection(collection, qdrant_url)
            resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
            resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
            try:
                _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
                _early_ids = list({user_entity_id_for_query} | {f["subject"] for f in resolved_direct + resolved_baseline} | {f["object"] for f in resolved_direct + resolved_baseline} | resolved_entity_ids)
                attributes = _fetch_attributes(_attr_db, user_id, _early_ids, max_sensitivity="private")
                _attr_db.close()
            except Exception:
                pass
            attr_facts = _attributes_to_facts(attributes)
            log.info("query.404_attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
            resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
            merged_facts = resolved_direct + resolved_baseline + resolved_attr_facts
            log.info("query.404_merged", attr_count=len(resolved_attr_facts), total=len(merged_facts))
            # Populate preferred_names with all entities in merged facts
            if registry:
                for f in merged_facts:
                    if f["subject"] not in preferred_names and f["subject"] != user_entity_id_for_query:
                        preferred_names[f["subject"]] = registry.get_preferred_name(user_id, f["subject"]) or f["subject"].title()
                    if f["object"] not in preferred_names and f["object"] != user_entity_id_for_query:
                        preferred_names[f["object"]] = registry.get_preferred_name(user_id, f["object"]) or f["object"].title()
            return {
                "status": "ok",
                "facts": merged_facts,
                "preferred_names": preferred_names,
                "canonical_identity": canonical_identity,
                "attributes": attributes,
            }
        if resp.status_code != 200:
            log.warning("query.qdrant_error", status=resp.status_code, collection=collection)
            resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
            resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
            try:
                _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
                _early_ids = list({user_entity_id_for_query} | {f["subject"] for f in resolved_direct + resolved_baseline} | {f["object"] for f in resolved_direct + resolved_baseline} | resolved_entity_ids)
                attributes = _fetch_attributes(_attr_db, user_id, _early_ids, max_sensitivity="private")
                _attr_db.close()
            except Exception:
                pass
            attr_facts = _attributes_to_facts(attributes)
            log.info("query.error_attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
            resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
            merged_facts = resolved_direct + resolved_baseline + resolved_attr_facts
            log.info("query.error_merged", attr_count=len(resolved_attr_facts), total=len(merged_facts))
            # Populate preferred_names with all entities in merged facts
            if registry:
                for f in merged_facts:
                    if f["subject"] not in preferred_names and f["subject"] != user_entity_id_for_query:
                        preferred_names[f["subject"]] = registry.get_preferred_name(user_id, f["subject"]) or f["subject"].title()
                    if f["object"] not in preferred_names and f["object"] != user_entity_id_for_query:
                        preferred_names[f["object"]] = registry.get_preferred_name(user_id, f["object"]) or f["object"].title()
            return {
                "status": "ok",
                "facts": merged_facts,
                "preferred_names": preferred_names,
                "canonical_identity": canonical_identity,
                "attributes": attributes,
            }

        qdrant_facts = [
            {
                "subject": h["payload"].get("subject"),
                "object": h["payload"].get("object"),
                "rel_type": h["payload"].get("rel_type"),
                "provenance": h["payload"].get("provenance"),
                "confidence": h["payload"].get("confidence", 1.0),
                "category": _REL_TYPE_META.get(h["payload"].get("rel_type"), {}).get("category"),
                "fact_state": (
                    "long_term" if h["payload"].get("fact_class") in ("A", None)
                    else "staged" if h["payload"].get("fact_class") == "B"
                    else "ephemeral"
                ),
                "fact_class": h["payload"].get("fact_class", "A"),
            }
            for h in resp.json().get("result", [])
            if h.get("payload")
        ]
        log.info("query.ok", collection=collection, hits=len(qdrant_facts))

        # Resolve display names for all facts before merging
        resolved_baseline = _resolve_display_names(baseline_facts, registry, user_id, user_entity_id_for_query) if registry else baseline_facts
        resolved_direct = _resolve_display_names(direct_facts, registry, user_id, user_entity_id_for_query) if registry else direct_facts
        resolved_qdrant = _resolve_display_names(qdrant_facts, registry, user_id, user_entity_id_for_query) if registry else qdrant_facts

        # Merge: Postgres facts are authoritative, Qdrant adds associative context
        # Deduplicate on (subject, object, rel_type) — Postgres wins on conflict
        pg_keys = {(f["subject"], f["object"], f["rel_type"]) for f in resolved_direct}
        merged_facts = resolved_direct.copy()
        for f in resolved_qdrant:
            key = (f["subject"], f["object"], f["rel_type"])
            if key not in pg_keys:
                merged_facts.append(f)
                pg_keys.add(key)

        # Merge baseline personal facts (location, attributes) — always present for
        # known identities regardless of whether Qdrant or graph traversal returned them.
        for f in resolved_baseline:
            key = (f["subject"], f["object"], f["rel_type"])
            if key not in pg_keys:
                merged_facts.append(f)
                pg_keys.add(key)

        # Extract entity IDs from PRE-RESOLUTION facts to get UUIDs, not display names
        pre_resolution_fact_ids = {user_entity_id_for_query} | resolved_entity_ids
        for f in direct_facts + baseline_facts + qdrant_facts:
            if _UUID_PATTERN.match(f.get("subject", "")):
                pre_resolution_fact_ids.add(f["subject"])
            if _UUID_PATTERN.match(f.get("object", "")):
                pre_resolution_fact_ids.add(f["object"])

        try:
            _attr_db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
            attributes = _fetch_attributes(_attr_db, user_id, list(pre_resolution_fact_ids), max_sensitivity="private")
            _attr_db.close()
        except Exception as _ae:
            log.warning("query.qdrant_attributes_failed", error=str(_ae))
            attributes = {}

        # Merge user entity_attributes as facts (born_on, age, height, etc.)
        attr_facts = _attributes_to_facts(attributes)
        log.info("query.attributes_to_facts", count=len(attr_facts), attributes_keys=list(attributes.keys()) if attributes else [])
        resolved_attr_facts = _resolve_display_names(attr_facts, registry, user_id, user_entity_id_for_query) if registry else attr_facts
        attr_added = 0
        for f in resolved_attr_facts:
            key = (f["subject"], f["object"], f["rel_type"])
            if key not in pg_keys:
                merged_facts.append(f)
                pg_keys.add(key)
                attr_added += 1
        log.info("query.attributes_merged", added=attr_added, total_after=len(merged_facts))

        # Populate preferred_names only with entities that were registered as
        # UUID surrogates (real people/places), not fact-value strings like
        # "systems analyst" or "156 cedar st s".
        # Build the set of real entity UUIDs from pre-resolution PostgreSQL facts.
        # # NO RECURSIVE MATCHING — _UUID_RE used against pre-extracted values only
        _real_entity_ids: set[str] = {user_entity_id_for_query}
        for f in direct_facts + baseline_facts:
            if _UUID_PATTERN.match(f.get("subject", "")):
                _real_entity_ids.add(f["subject"])
            if _UUID_PATTERN.match(f.get("object", "")):
                _real_entity_ids.add(f["object"])
        # Build mapping: UUID → display name (from entity_aliases)
        _entity_display: dict[str, str] = {}
        if registry:
            for eid in _real_entity_ids:
                display = registry.get_preferred_name(user_id, eid)
                if display and display != eid:
                    _entity_display[eid] = display

        if registry:
            for f in merged_facts:
                # Subject: add if it matches a registered entity UUID OR its display name
                # matches a known registered entity's display name
                subj = f["subject"]
                if subj not in preferred_names and subj != user_entity_id_for_query:
                    if _UUID_PATTERN.match(subj) or subj in _entity_display.values():
                        preferred_names[subj] = registry.get_preferred_name(user_id, subj) or subj.title()
                obj = f["object"]
                if obj not in preferred_names and obj != user_entity_id_for_query:
                    if _UUID_PATTERN.match(obj) or obj in _entity_display.values():
                        preferred_names[obj] = registry.get_preferred_name(user_id, obj) or obj.title()

        log.info("query.merged", pg_hits=len(pg_keys), baseline=len(resolved_baseline), total=len(merged_facts))

        return {
            "status": "ok",
            "facts": merged_facts,
            "preferred_names": preferred_names,
            "canonical_identity": canonical_identity,
            "attributes": attributes,
        }
    except Exception as e:
        log.error("query.failed", error=str(e))
        return {
            "status": "ok",
            "facts": [],
            "preferred_names": preferred_names,
            "canonical_identity": canonical_identity,
            "attributes": {},
        }
    finally:
        if db:
            db.close()

@app.post("/retract", response_model=RetractResponse)
def retract_fact(req: RetractRequest):
    try:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        manager = FactStoreManager(db)

        mode = "supersede"
        note = None
        if req.rel_type:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT correction_behavior FROM rel_types WHERE rel_type = %s",
                    (req.rel_type.lower(),),
                )
                row = cur.fetchone()
                if row:
                    mode = row[0]
            if mode == "immutable":
                return RetractResponse(
                    status="rejected", retracted=0, mode="immutable",
                    note=f"{req.rel_type} is immutable and cannot be retracted",
                )

        with db.cursor() as cur:
            affected_ids = manager.retract(
                cur, req.user_id, req.subject, req.rel_type, req.old_value, mode
            )
            db.commit()

        if affected_ids:
            collection = derive_collection(req.user_id)
            qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
            _delete_from_qdrant(affected_ids, collection, qdrant_url)

        # Clean up entity_aliases for pref_name hard-delete
        if req.rel_type and req.rel_type.lower() == "pref_name" and mode == "hard_delete":
            try:
                with db.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM entity_aliases
                        WHERE entity_id = %s
                          AND user_id = %s
                          AND alias = %s
                          AND is_preferred = true
                        """,
                        (req.subject, req.user_id, req.old_value)
                    )
                db.commit()
            except Exception as e:
                log.warning("retract.entity_aliases_cleanup_failed",
                            rel_type=req.rel_type, subject_id=req.subject, error=str(e))

        return RetractResponse(status="ok", retracted=len(affected_ids), mode=mode, note=note)
    except Exception as e:
        log.error("retract.error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'db' in locals():
            db.close()


@app.post("/store_context", response_model=StoreContextResponse)
def store_context(req: StoreContextRequest):
    """
    Store unstructured text directly to Qdrant when no typed edges can be extracted.
    No WGM gate, no Postgres write, direct Qdrant upsert only.
    """
    try:
        qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
        qwen_api_url = os.environ.get("QWEN_API_URL", "http://localhost:11434/v1/chat/completions")

        collection = derive_collection(req.user_id)

        # Ensure collection exists
        if not ensure_collection(collection, qdrant_url):
            log.error("store_context.collection_ensure_failed", collection=collection)
            raise HTTPException(status_code=500, detail="Collection unavailable")

        # Embed the text
        vector = embed_text(req.text, qwen_api_url, timeout=10.0, fallback=False)
        if vector is None:
            log.error("store_context.embed_failed", user_id=req.user_id, text_length=len(req.text))
            raise HTTPException(status_code=500, detail={"status": "error", "point_id": ""})

        # Generate point ID
        point_id = str(uuid.uuid4())

        # Upsert to Qdrant
        response = httpx.put(
            f"{qdrant_url}/collections/{collection}/points",
            json={
                "points": [
                    {
                        "id": point_id,
                        "vector": vector,
                        "payload": {
                            "text": req.text,
                            "source": req.source,
                            "context_type": req.context_type,
                            "user_id": req.user_id,
                            "subject": "user",
                            "rel_type": "context",
                            "object": req.text[:120],
                            "fact_class": "C",
                            "confidence": 0.4,
                        },
                    }
                ]
            },
            timeout=10.0,
        )

        if response.status_code != 200:
            log.error(
                "store_context.upsert_failed",
                user_id=req.user_id,
                status=response.status_code,
                text_length=len(req.text),
            )
            raise HTTPException(status_code=500, detail="Qdrant upsert failed")

        log.info(
            "store_context.stored",
            user_id=req.user_id,
            point_id=point_id,
            context_type=req.context_type,
            text_length=len(req.text),
        )

        return StoreContextResponse(status="stored", point_id=point_id)

    except HTTPException:
        raise
    except Exception as e:
        log.error("store_context.error", error=str(e), user_id=req.user_id)
        raise HTTPException(status_code=500, detail=str(e))