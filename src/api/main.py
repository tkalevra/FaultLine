import os
from contextlib import asynccontextmanager

import psycopg2
import structlog
from fastapi import Depends, FastAPI, HTTPException

from src.context_packager import build_audit_context
from src.fact_store.store import FactStoreManager
from src.gli_ner import extract_entities
from src.gli_ner.extractor import load_default_model
from src.schema_oracle import resolve_entities
from src.wgm.gate import WGMValidationGate

from .models import EdgeInput, EntityResult, FactResult, IngestRequest, IngestResponse, QueryRequest

log = structlog.get_logger()

DIRECTED_RELATIONSHIPS = frozenset({
    "parent_of", "child_of", "works_for", "created_by",
    "kills", "part_of", "is_a", "reports_to",
})


def infer_rel_type(a: dict, b: dict) -> str:
    """Infer relationship type based on entity labels."""
    label_a = a.get("type", "").lower()
    label_b = b.get("type", "").lower()
    if label_a == "person" and label_b == "person":
        return "related_to"
    if label_a == "person" and label_b == "organization":
        return "works_for"
    if label_a == "organization" and label_b == "person":
        return "created_by"
    return "related_to"


def deduplicate_directed(edges: list) -> list:
    """Remove duplicate edges and reverse-edge duplicates from the batch.
    
    For directed relationships, if (O, S, R) exists in the batch, (S, O, R) is skipped.
    Comparison is case-insensitive; entities matching themselves are filtered out.
    """
    seen = set()
    result = []
    for edge in edges:
        s = edge.subject.lower()
        o = edge.object.lower()
        r = edge.rel_type.lower()
        if s == o:
            continue
        if r in DIRECTED_RELATIONSHIPS:
            if (o, s, r) in seen:
                continue
        seen.add((s, o, r))
        result.append(edge)
    return result

_gliner_model = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gliner_model
    log.info("startup.gliner_load")
    _gliner_model = load_default_model()
    log.info("startup.gliner_ready")
    yield
    _gliner_model = None


app = FastAPI(title="FaultLine WGM", version="0.1.0", lifespan=lifespan)


def get_gliner_model():
    if _gliner_model is None:
        raise HTTPException(status_code=503, detail="GliNER model not ready")
    return _gliner_model


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest, model=Depends(get_gliner_model)):
    log.info("ingest.start", text_len=len(req.text), source=req.source)

    # Step 1: Extract entities
    entities = extract_entities(req.text, model)
    log.info("ingest.extracted", count=len(entities))

    # Step 2: Resolve canonical IDs — dynamically allow whatever GliNER detected
    known_types = list({e["label"] for e in entities} | set(req.known_types))
    oracle_input = {
        "entities": [{"entity": e["entity"], "type": e["label"]} for e in entities]
    }
    try:
        resolution = resolve_entities(
            oracle_input,
            model=None,
            context={"known_types": known_types, "registry": {}},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    resolved = resolution["resolution"]["resolved"]
    entity_results = [
        EntityResult(
            entity=r["entity"],
            label=r["type"],
            canonical_id=r["canonical_id"],
        )
        for r in resolved
    ]

    # Step 3: Validate + commit edges
    facts: list[FactResult] = []
    committed = 0
    overall_status = "extracted"

    # Determine edge source: explicit edges or inferred
    edges_to_process = []

    if req.edges:
        # Explicit edges provided by caller
        edges_to_process = req.edges
    elif len(resolved) >= 2:
        # No explicit edges but enough entities to infer relationships
        candidates = []
        for i in range(len(resolved) - 1):
            a = resolved[i]
            b = resolved[i + 1]
            candidates.append(
                EdgeInput(
                    subject=a["entity"].lower(),
                    object=b["entity"].lower(),
                    rel_type=infer_rel_type(a, b),
                )
            )
        log.info("ingest.inferred", candidate_count=len(candidates))
        edges_to_process = candidates

    # Process edges (explicit or inferred)
    if edges_to_process:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="POSTGRES_DSN not configured")

        db = psycopg2.connect(dsn)
        try:
            gate = WGMValidationGate(db)
            manager = FactStoreManager(db)
            rows_to_commit = []

            edges = deduplicate_directed(edges_to_process)
            for edge in edges:
                validation = gate.validate_edge(edge.subject, edge.object, edge.rel_type)
                status = validation["status"]
                facts.append(
                    FactResult(
                        subject=edge.subject,
                        object=edge.object,
                        rel_type=edge.rel_type,
                        status=status,
                    )
                )
                if status == "valid":
                    rows_to_commit.append(
                        (edge.subject, edge.object, edge.rel_type, req.source)
                    )

            # ================================================================
            # COMMIT INTEGRITY — DO NOT MODIFY THIS BLOCK WITHOUT REVIEW
            # Facts are committed to Postgres here. The re-embedder service
            # picks them up via qdrant_synced=false and writes to Qdrant.
            # subject_id and object_id must be the ACTUAL ENTITY TEXT (lowercase),
            # not canonical_id slugs like "person-0". canonical_id is for
            # internal deduplication only and must never be used as a graph node key.
            # Changing the values stored here will break /query token matching
            # and the re-embedder payload permanently.
            # ================================================================
            if rows_to_commit:
                if log.isEnabledFor(structlog.DEBUG):
                    for sub, obj, rel, _ in rows_to_commit:
                        log.debug("ingest.committing", subject=sub, object=obj, rel_type=rel)
                committed = manager.commit(rows_to_commit)

            statuses = {f.status for f in facts}
            if "conflict" in statuses:
                overall_status = "conflict"
            elif "novel" in statuses:
                overall_status = "novel"
            else:
                overall_status = "valid"
        finally:
            db.close()

    log.info("ingest.done", status=overall_status, committed=committed)
    return IngestResponse(
        status=overall_status,
        committed=committed,
        entities=entity_results,
        facts=facts,
    )


@app.post("/query")
def query(request: QueryRequest):
    log.info("query.start", text_len=len(request.text))

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise HTTPException(status_code=503, detail="POSTGRES_DSN not configured")

    tokens = request.text.lower().split()
    like_patterns = ['%' + t + '%' for t in tokens]

    try:
        db = psycopg2.connect(dsn)
        try:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT subject_id, object_id, rel_type, provenance
                    FROM facts
                    WHERE subject_id = ANY(%s)
                    OR object_id = ANY(%s)
                    OR subject_id LIKE ANY(%s)
                    OR object_id LIKE ANY(%s)
                    LIMIT 20
                    """,
                    (tokens, tokens, like_patterns, like_patterns)
                )
                rows = cur.fetchall()

            # Deduplicate by (subject_id, object_id, rel_type)
            seen = set()
            facts = []
            for subject_id, object_id, rel_type, provenance in rows:
                key = (subject_id, object_id, rel_type)
                if key not in seen:
                    seen.add(key)
                    facts.append({
                        "subject": subject_id,
                        "object": object_id,
                        "rel_type": rel_type,
                        "provenance": provenance
                    })

            log.info("query.done", fact_count=len(facts))
            return {"status": "ok", "facts": facts}
        finally:
            db.close()
    except Exception as exc:
        log.error("query.failed", error=str(exc))
        return {"status": "error", "facts": []}
