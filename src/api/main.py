import os
from contextlib import asynccontextmanager
import httpx
import psycopg2
import structlog
from fastapi import Depends, FastAPI, HTTPException
from src.fact_store.store import FactStoreManager
from src.re_embedder.embedder import derive_collection, embed_text
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate
from .models import EdgeInput, EntityResult, FactResult, IngestRequest, IngestResponse, QueryRequest

log = structlog.get_logger()

# GLiNER2 schema — dict form: {label: description}
# label   = the key GLiNER2 uses in returned results; must be lowercase_with_underscores
#            for relations so they match WGM gate slugs directly (no lookup needed)
# description = natural language guidance that improves model accuracy
ENTITY_SCHEMA = {
    "person":       "a person or individual",
    "organization": "a company, business, or organization",
    "location":     "a city, country, or geographic location",
}

# Maps GLiNER2 entity label → DB slug (title-cased for resolve_entities)
ENTITY_LABEL_TO_DB = {
    "person":       "Person",
    "organization": "Organization",
    "location":     "Location",
}

# Relation labels ARE the DB slugs — no secondary mapping needed
RELATION_SCHEMA = {
    "parent_of":    "is the parent, father, or mother of",
    "child_of":     "is the child, son, or daughter of",
    "spouse":       "is married to or is the spouse of",
    "sibling_of":   "is the sibling, brother, or sister of",
    "also_known_as":"is also known as, goes by, or has the nickname",
    "works_for":    "works for or is employed by",
}

def extract_entities_and_relations(text: str, model) -> tuple[list[dict], list[EdgeInput]]:
    if model is None: return [], []
    try:
        schema = (model.create_schema()
            .entities(ENTITY_SCHEMA)
            .relations(RELATION_SCHEMA)
        )
        results = model.extract(text, schema)

        entities = []
        for label, db_label in ENTITY_LABEL_TO_DB.items():
            for e in results.get(label, []):
                val = e["text"] if isinstance(e, dict) else str(e)
                entities.append({"entity": val.lower().strip(), "label": db_label})

        relation_edges = []
        def get_text(o): return o.get("text", "") if isinstance(o, dict) else str(o)
        for rel_label, pairs in results.get("relation_extraction", {}).items():
            if rel_label not in RELATION_SCHEMA:
                continue
            for pair in pairs:
                if isinstance(pair, (tuple, list)): h, t = pair[0], pair[1]
                else: h, t = pair.get("head"), pair.get("tail")
                sub, obj = get_text(h).lower().strip(), get_text(t).lower().strip()
                if sub and obj and sub != obj:
                    relation_edges.append(EdgeInput(subject=sub, object=obj, rel_type=rel_label))
        return entities, relation_edges
    except Exception as e:
        log.error("ingest.gliner2_failed", error=str(e))
        return [], []

_gliner2_model = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gliner2_model
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
    entities, inferred_relations = extract_entities_and_relations(req.text, model)
    resolution = resolve_entities({"entities": [{"entity": e["entity"], "type": e["label"]} for e in entities]}, 
                                  context={"known_types": ["Person", "Organization", "Location"]})
    resolved = resolution["resolution"]["resolved"]
    
    edges = req.edges or inferred_relations or []
    facts, committed = [], 0
    if edges:
        db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
        try:
            gate, manager = WGMValidationGate(db), FactStoreManager(db)
            rows = []
            for edge in edges:
                if edge.subject == edge.object: continue
                status = gate.validate_edge(edge.subject, edge.object, edge.rel_type)["status"]
                facts.append(FactResult(subject=edge.subject, object=edge.object, rel_type=edge.rel_type, status=status))
                if status == "valid": rows.append((req.user_id, edge.subject, edge.object, edge.rel_type, req.source))
            if rows: committed = manager.commit(rows)
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
        return {"status": "ok", "facts": []}

    try:
        resp = httpx.post(
            f"{qdrant_url}/collections/{collection}/points/search",
            json={"vector": vector, "limit": 10, "with_payload": True, "score_threshold": 0.3},
            timeout=10.0,
        )
        if resp.status_code == 404:
            return {"status": "ok", "facts": []}
        if resp.status_code != 200:
            log.warning("query.qdrant_error", status=resp.status_code, collection=collection)
            return {"status": "ok", "facts": []}

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
        return {"status": "ok", "facts": facts}
    except Exception as e:
        log.error("query.failed", error=str(e))
        return {"status": "ok", "facts": []}