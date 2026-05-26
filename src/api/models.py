from typing import Optional

from pydantic import BaseModel


class EdgeInput(BaseModel):
    subject: str
    object: str
    rel_type: str
    is_preferred_label: bool = False
    is_correction: bool = False
    confidence: Optional[float] = None  # User corrections: 1.0. Default: None (ingest computes based on provenance)
    fact_provenance: str = "llm_inferred"  # user_stated | llm_inferred | confirmed
    subject_type: Optional[str] = None  # Person, Animal, Organization, Location, Object, Concept (from GLiNER2)
    object_type: Optional[str] = None  # Person, Animal, Organization, Location, Object, Concept (from GLiNER2)
    definition: Optional[str] = None  # semantic definition of rel_type, LLM-generated at extraction time (dprompt-85)
    temporal_context: Optional[str] = None  # dBug-055: Text qualifier ("in 4 days", "next Tuesday", etc.)
    temporal_context_resolved_at: Optional[str] = None  # ISO 8601 timestamp when temporal expression resolves


class ExtractContext(BaseModel):
    known_entities: list[dict] | None = None  # [{"name":"${USER}","type":"Person","uuid":"..."},...]
    ontology_hints: list[str] | None = None    # ["has_injury → Person,body_part", ...]
    user_profile: str | None = None            # "User: ${USER}. Family: spouse=${SPOUSE}..."


class IngestRequest(BaseModel):
    text: str
    source: str = "api"
    edges: list[EdgeInput] | None = None
    known_types: list[str] = ["Person", "Organization", "Location", "Event", "Concept"]
    user_id: Optional[str] = "anonymous"
    chat_id: Optional[str] = None  # dBug-016: Preserve OpenWebUI conversation context
    context: ExtractContext | None = None  # Optional context enrichment for /extract (dBug-018)
    memory_facts: list[dict] | None = None  # Prior facts for pronoun resolution during extraction
    is_correction: bool = False  # dBug-041: User correction flag — bypass blocklist validation
    idempotency_key: Optional[str] = None  # Phase 2: Deduplicate retried requests via idempotency cache


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
    definition: Optional[str] = None  # Natural language template from rel_types table (e.g., "X is Y's spouse")


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
    # Metadata for classification routing (dprompt-97)
    head_types: Optional[list[str]] = None  # subject entity types (e.g., ['Person', 'Organization'])
    tail_types: Optional[list[str]] = None  # object entity/value types (e.g., ['SCALAR'], ['Person'])
    is_symmetric: Optional[bool] = None     # bidirectional (spouse, knows, same_as)
    inverse_rel_type: Optional[str] = None  # reverse relationship (parent_of ↔ child_of)
    is_hierarchy_rel: Optional[bool] = None # classification/taxonomy (instance_of, subclass_of)


class RetractRequest(BaseModel):
    user_id: str
    subject: Optional[str] = None
    rel_type: Optional[str] = None
    old_value: Optional[str] = None
    scope: Optional[dict] = None


class RetractResponse(BaseModel):
    status: str
    retracted: int
    mode: str
    note: Optional[str] = None
    scope_level: Optional[str] = None


class StoreContextRequest(BaseModel):
    text: str
    user_id: str = "anonymous"
    source: str = "openwebui"
    context_type: str = "unstructured"


class StoreContextResponse(BaseModel):
    status: str  # "stored" or "error"
    point_id: str  # Qdrant point UUID


class RewriteRequest(BaseModel):
    """Request for LLM-based fact extraction (triple rewriting).
    Called by OpenWebUI Filter instead of hitting OpenWebUI's LLM directly.
    FaultLine controls which LLM to use and manages all LLM configuration."""
    text: str
    user_id: Optional[str] = "anonymous"
    chat_id: Optional[str] = None  # dBug-016: Preserve OpenWebUI conversation context
    messages: list[dict] | None = None  # Prior conversation context
    typed_entities: list[dict] | None = None  # Pre-extracted entities from GLiNER2
    memory_facts: list[dict] | None = None  # Prior facts for pronoun resolution


class RewriteResponse(BaseModel):
    """LLM-extracted edges (facts) from input text."""
    status: str  # "success" or "error"
    edges: list[EdgeInput] = []  # Extracted facts with types


class FactCorrectionRequest(BaseModel):
    """User correction: old fact is wrong, new fact is right.
    Surgical update: only supersede one specific fact, re-ingest through WGM gate.
    """
    text: str  # "Fraggle is a dog not a bunny"
    user_id: str  # User UUID (will be validated against authenticated user)
    context_facts: Optional[list[dict]] = None  # Recent facts for entity resolution
    idempotency_key: Optional[str] = None  # Deduplicate retried correction requests (via Redis)


class FactCorrectionResponse(BaseModel):
    """Surgical correction result."""
    status: str  # "corrected", "failed", "disambiguation_needed"
    subject_uuid: Optional[str] = None
    subject_name: Optional[str] = None
    old_rel_type: Optional[str] = None
    old_value: Optional[str] = None
    new_rel_type: Optional[str] = None
    new_value: Optional[str] = None
    dimension: Optional[str] = None  # SCALAR | RELATIONAL | HIERARCHICAL | SUBJECT | REL_TYPE | ENTITY_TYPE
    confidence: float = 0.0
    facts_superseded: int = 0
    hierarchies_modified: list[str] = []
    message: Optional[str] = None
    error: Optional[str] = None
