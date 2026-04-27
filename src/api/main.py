import os
from contextlib import asynccontextmanager
import psycopg2
import structlog
from fastapi import Depends, FastAPI, HTTPException
from src.fact_store.store import FactStoreManager
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate
from .models import EdgeInput, EntityResult, FactResult, IngestRequest, IngestResponse, QueryRequest

log = structlog.get_logger()

# Mapping Natural Language prompts to DB Slugs
ENTITY_PROMPTS = {
    "a human being": "Person",
    "a company or organization": "Organization",
    "a physical location": "Location"
}

RELATION_PROMPTS = {
    "is the parent of": "parent_of",
    "is the child of": "child_of",
    "is the spouse of": "spouse",
    "is also known as": "also_known_as",
    "goes by the nickname": "also_known_as",
    "prefers to be called": "also_known_as",
    "works for": "works_for"
}

DIRECTED_RELATIONSHIPS = frozenset({"parent_of", "child_of", "works_for", "part_of"})

def extract_entities_and_relations(text: str, model) -> tuple[list[dict], list[EdgeInput]]:
    if model is None: return [], []
    try:
        schema = (model.create_schema()
            .entities(list(ENTITY_PROMPTS.keys()))
            .relations(list(RELATION_PROMPTS.keys()))
        )
        results = model.extract(text, schema)
        
        entities = []
        for prompt, db_label in ENTITY_PROMPTS.items():
            for e in results.get(prompt, []):
                val = e["text"] if isinstance(e, dict) else str(e)
                entities.append({"entity": val.lower().strip(), "label": db_label})

        relation_edges = []
        rel_results = results.get("relation_extraction", {})
        for prompt_rel, pairs in rel_results.items():
            db_rel = RELATION_PROMPTS.get(prompt_rel, "related_to")
            for pair in pairs:
                if isinstance(pair, (tuple, list)): h, t = pair[0], pair[1]
                else: h, t = pair.get("head"), pair.get("tail")
                
                def get_t(o): return o.get("text", "") if isinstance(o, dict) else str(o)
                sub, obj = get_t(h).lower().strip(), get_t(t).lower().strip()
                
                if sub and obj and sub != obj:
                    relation_edges.append(EdgeInput(subject=sub, object=obj, rel_type=db_rel))
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
    db = psycopg2.connect(os.environ.get("POSTGRES_DSN"))
    try:
        tokens = request.text.lower().split()
        with db.cursor() as cur:
            cur.execute("SELECT subject_id, object_id, rel_type, provenance FROM facts WHERE user_id = %s AND (subject_id = ANY(%s) OR object_id = ANY(%s)) LIMIT 20", (request.user_id or "anonymous", tokens, tokens))
            return {"status": "ok", "facts": [{"subject": r[0], "object": r[1], "rel_type": r[2], "provenance": r[3]} for r in cur.fetchall()]}
    finally: db.close()