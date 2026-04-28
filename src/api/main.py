import os
from contextlib import asynccontextmanager
import httpx
import psycopg2
import structlog
from fastapi import Depends, FastAPI, HTTPException
from src.fact_store.store import FactStoreManager
from src.re_embedder.embedder import derive_collection, embed_text, ensure_collection
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate
from .models import EdgeInput, EntityResult, FactResult, IngestRequest, IngestResponse, QueryRequest

log = structlog.get_logger()

_PREFERENCE_SIGNALS = {
    "goes by", "prefers to be called", "preferred name", "please call me",
    "call me", "known as", "my name is"
}

def _detect_preference_signal(text: str) -> bool:
    text_lower = text.lower()
    return any(signal in text_lower for signal in _PREFERENCE_SIGNALS)

_gliner2_model = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gliner2_model

    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    default_collection = os.environ.get("QDRANT_COLLECTION", "faultline-test")
    log.info("startup.qdrant_collection_check", collection=default_collection)
    if ensure_collection(default_collection, qdrant_url):
        log.info("startup.qdrant_collection_ready", collection=default_collection)
    else:
        log.error("startup.qdrant_collection_failed", collection=default_collection)

    log.info("startup.gliner2_loading")
    try:
        from gliner2 import GLiNER2
        _gliner2_model = GLiNER2.from_pretrained("fastino/gliner2-base-v1")
        log.info("startup.gliner2_ready")
    except Exception as e:
        log.error("startup.gliner2_failed", error=str(e))
    yield
    _gliner2_model = None

app = FastAPI(title="FaultLine WGM", lifespan=lifespan)

@app.get("/health")
def health():
    if _gliner2_model is None:
        raise HTTPException(status_code=503, detail="Model loading")
    return {"status": "ok"}

@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest, model=Depends(lambda: _gliner2_model)):
    inferred_relations = []
    if model is not None:
        try:
            schema = {
                "facts": [
                    "subject::str::The full proper name of the first entity in the relationship. Never a pronoun.",
                    "object::str::The full proper name of the second entity in the relationship. Never a pronoun.",
                    "rel_type::[is_a|part_of|created_by|works_for|parent_of|child_of|spouse|sibling_of|also_known_as|related_to|likes|dislikes|prefers|has_gender|lives_in|born_in|born_on|nationality|educated_at|occupation|owns|located_in|knows|friend_of|met|age|located_at]::str::The relationship type from subject to object. Choose the most specific type that fits.",
                ]
            }
            result = model.extract_json(req.text, schema)
            inferred_relations = [
                EdgeInput(
                    subject=fact["subject"].lower().strip(),
                    object=fact["object"].lower().strip(),
                    rel_type=fact["rel_type"].lower().strip()
                )
                for fact in result.get("facts", [])
                if fact.get("subject") and fact.get("object") and fact.get("rel_type")
            ]
        except Exception as e:
            log.error("ingest.gliner2_failed", error=str(e))

    resolution = resolve_entities({"entities": []},
                                  context={"known_types": ["Person", "Organization", "Location"]})
    resolved = resolution["resolution"]["resolved"]
    
    edges = req.edges or inferred_relations or []
    facts, committed = [], 0
    if edges:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        try:
            gate, manager = WGMValidationGate(db), FactStoreManager(db)
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