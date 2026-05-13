from typing import Optional

from pydantic import BaseModel


class EdgeInput(BaseModel):
    subject: str
    object: str
    rel_type: str
    is_preferred_label: bool = False
    is_correction: bool = False
    fact_provenance: str = "llm_inferred"  # user_stated | llm_inferred | confirmed
    subject_type: Optional[str] = None  # Person, Animal, Organization, Location, Object, Concept (from GLiNER2)
    object_type: Optional[str] = None  # Person, Animal, Organization, Location, Object, Concept (from GLiNER2)


class ExtractContext(BaseModel):
    known_entities: list[dict] | None = None  # [{"name":"chris","type":"Person","uuid":"..."},...]
    ontology_hints: list[str] | None = None    # ["has_injury → Person,body_part", ...]
    user_profile: str | None = None            # "User: chris. Family: spouse=mars..."


class IngestRequest(BaseModel):
    text: str
    source: str = "api"
    edges: list[EdgeInput] | None = None
    known_types: list[str] = ["Person", "Organization", "Location", "Event", "Concept"]
    user_id: Optional[str] = "anonymous"
    context: ExtractContext | None = None  # Optional context enrichment for /extract (dBug-018)


class QueryRequest(BaseModel):
    text: str
    source: Optional[str] = "openwebui"
    user_id: Optional[str] = "anonymous"


class EntityResult(BaseModel):
    entity: str
    label: str
    canonical_id: str


class FactResult(BaseModel):
    subject: str
    object: str
    rel_type: str
    status: str
    fact_class: str = "A"  # A, B, or C
    provenance: str = "llm_inferred"


class IngestResponse(BaseModel):
    status: str
    committed: int
    staged: int = 0  # Facts written to staged_facts (Class B + C)
    entities: list[EntityResult]
    facts: list[FactResult]


class RelTypeRequest(BaseModel):
    rel_type: str
    label: str
    subject_role: str = "entity"
    object_role: str = "entity"
    correction_behavior: str = "supersede"
    wikidata_pid: Optional[str] = None


class RetractRequest(BaseModel):
    user_id: str
    subject: str
    rel_type: Optional[str] = None
    old_value: Optional[str] = None


class RetractResponse(BaseModel):
    status: str
    retracted: int
    mode: str
    note: Optional[str] = None


class StoreContextRequest(BaseModel):
    text: str
    user_id: str = "anonymous"
    source: str = "openwebui"
    context_type: str = "unstructured"


class StoreContextResponse(BaseModel):
    status: str  # "stored" or "error"
    point_id: str  # Qdrant point UUID
