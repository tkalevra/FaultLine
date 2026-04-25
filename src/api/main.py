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

from .models import EntityResult, FactResult, IngestRequest, IngestResponse

log = structlog.get_logger()

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

    # Step 3: Validate + commit edges (only if caller provided explicit edges)
    facts: list[FactResult] = []
    committed = 0
    overall_status = "extracted"

    if req.edges:
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="POSTGRES_DSN not configured")

        db = psycopg2.connect(dsn)
        try:
            gate = WGMValidationGate(db)
            manager = FactStoreManager(db)
            rows_to_commit = []

            for edge in req.edges:
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

            if rows_to_commit:
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
