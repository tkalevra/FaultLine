from typing import Optional

from pydantic import BaseModel


class EdgeInput(BaseModel):
    subject: str
    object: str
    rel_type: str


class IngestRequest(BaseModel):
    text: str
    source: str = "api"
    edges: list[EdgeInput] | None = None
    known_types: list[str] = ["Person", "Organization", "Location", "Event", "Concept"]
    user_id: Optional[str] = "anonymous"


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


class IngestResponse(BaseModel):
    status: str
    committed: int
    entities: list[EntityResult]
    facts: list[FactResult]
