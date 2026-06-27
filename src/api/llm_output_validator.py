"""
Unified LLM Output Validator & Storage Router

Centralizes validation, confidence scoring, and storage routing for ALL LLM outputs in FaultLine.

Controls:
- Fact extraction → WGM validation
- Retraction signals → detection + storage
- Entity type inference → hierarchy management
- Novel patterns (rel_types, entity_types, signals) → evaluation + approval
- Context data → Qdrant storage

Provides:
- Unified confidence scoring algorithm (frequency + LLM confidence)
- Frequency tracking and semantic similarity matching
- Storage routing (direct → staged → rejected → expired)
- Lifecycle management (promotion, expiry, archival)
- Global metrics dashboard
- Structured logging of all decisions

Ref: dBug-046 generalized — unified LLM output control across all modules
"""
import logging
import os
from typing import Any, Optional, Union
from dataclasses import dataclass
from datetime import datetime, timedelta
import httpx
import structlog

log = structlog.get_logger()

# Embedding model name — PURE CONFIG, read from env (no code literal). Default lives in
# .env.example. Empty when unset → the embed call fail-safes (logs + returns no vector), it
# never crashes the validator.
_EMBEDDING_MODEL = (os.getenv("EMBEDDING_MODEL") or "").strip()


@dataclass
class ValidationResult:
    """Result of LLM output validation."""
    valid: bool
    storage_decision: str  # 'direct' | 'staged' | 'rejected' | 'hold'
    confidence: float  # [0.0, 1.0]
    reason: str
    frequency: int
    similar_patterns: list[dict] = None
    metadata: dict = None

    def __post_init__(self):
        if self.similar_patterns is None:
            self.similar_patterns = []
        if self.metadata is None:
            self.metadata = {}


class LLMOutputValidator:
    """
    Centralized validation and storage routing for all LLM outputs.

    Single source of truth for:
    - What LLM outputs are valid
    - Where they get stored (facts, staged_facts, rejection queue, etc.)
    - What confidence score they get
    - When they're promoted/expired
    - Global metrics across all output types
    """

    # Default thresholds (configurable per output type)
    DEFAULT_THRESHOLDS = {
        'fact': {'frequency': 1, 'approval': 0.8, 'similarity': 0.85},
        'retraction_signal': {'frequency': 2, 'approval': 0.75, 'similarity': 0.85},
        'correction_signal': {'frequency': 1, 'approval': 0.6, 'similarity': 0.85},  # dprompt-114
        'entity_type': {'frequency': 2, 'approval': 0.75, 'similarity': 0.85},
        'rel_type': {'frequency': 3, 'approval': 0.8, 'similarity': 0.85},
        'context': {'frequency': 1, 'approval': 0.4, 'similarity': 0.85},
    }

    # Output type → storage table mapping
    STORAGE_TABLES = {
        'fact': 'facts',
        'retraction_signal': 'retraction_signals',
        'correction_signal': 'correction_signals',  # dprompt-114
        'entity_type': 'entities',
        'rel_type': 'rel_types',
        'context': 'qdrant',  # Qdrant collection, not PostgreSQL
    }

    def __init__(self,
                 db_conn: Optional[Any] = None,
                 llm_endpoint: str = "http://localhost:8080",
                 thresholds: Optional[dict] = None):
        """
        Initialize the validator.

        Args:
            db_conn: Database connection (optional)
            llm_endpoint: LLM endpoint for embeddings
            thresholds: Override default thresholds per output_type
        """
        self.db_conn = db_conn
        self.llm_endpoint = llm_endpoint
        self.thresholds = {**self.DEFAULT_THRESHOLDS}
        if thresholds:
            self.thresholds.update(thresholds)

        # Global metrics
        self.metrics = {
            'fact': {'validated': 0, 'direct': 0, 'staged': 0, 'rejected': 0, 'held': 0},
            'retraction_signal': {'validated': 0, 'direct': 0, 'staged': 0, 'rejected': 0, 'held': 0},
            'correction_signal': {'validated': 0, 'direct': 0, 'staged': 0, 'rejected': 0, 'held': 0},  # dprompt-114
            'entity_type': {'validated': 0, 'direct': 0, 'staged': 0, 'rejected': 0, 'held': 0},
            'rel_type': {'validated': 0, 'approved': 0, 'mapped': 0, 'rejected': 0, 'held': 0},
            'context': {'validated': 0, 'direct': 0, 'rejected': 0},
        }

    async def validate_output(self,
                             output_type: str,
                             payload: dict,
                             source: str = 'llm',
                             llm_confidence: float = 0.8,
                             frequency: int = 1) -> ValidationResult:
        """
        Validate any LLM output for storage.

        Args:
            output_type: 'fact' | 'retraction_signal' | 'entity_type' | 'rel_type' | 'context'
            payload: Output data (structure varies by type)
            source: 'llm' | 'user' | 'engine'
            llm_confidence: LLM's confidence in the output [0.0, 1.0]
            frequency: How many times this pattern has been observed

        Returns: ValidationResult with decision and metadata
        """
        if output_type not in self.STORAGE_TABLES:
            return ValidationResult(
                valid=False,
                storage_decision='rejected',
                confidence=0.0,
                reason=f"unknown output_type: {output_type}",
                frequency=frequency
            )

        # Get thresholds for this output type
        thresholds = self.thresholds.get(output_type, self.DEFAULT_THRESHOLDS['fact'])

        # Compute unified confidence score
        confidence = self._compute_confidence(
            frequency=frequency,
            llm_confidence=llm_confidence,
            source=source,
            frequency_threshold=thresholds['frequency']
        )

        log.info(f"llm_output_validator.validate_output",
                 output_type=output_type, source=source,
                 frequency=frequency, llm_confidence=llm_confidence,
                 computed_confidence=confidence)

        # User outputs are always valid and direct
        if source == 'user':
            return ValidationResult(
                valid=True,
                storage_decision='direct',
                confidence=1.0,
                reason="user-stated, authoritative",
                frequency=frequency,
                metadata={'source': 'user'}
            )

        # Validate frequency threshold
        if frequency < thresholds['frequency']:
            return ValidationResult(
                valid=False,
                storage_decision='hold',
                confidence=confidence,
                reason=f"frequency={frequency} < threshold={thresholds['frequency']}",
                frequency=frequency
            )

        # Validate confidence threshold
        if confidence < thresholds['approval']:
            return ValidationResult(
                valid=False,
                storage_decision='staged',
                confidence=confidence,
                reason=f"confidence={confidence:.2f} < approval={thresholds['approval']:.2f}",
                frequency=frequency,
                metadata={'reason': 'low_confidence'}
            )

        # Check for similar patterns (semantic matching)
        similar_patterns = []
        if output_type in ['rel_type', 'retraction_signal', 'entity_type']:
            pattern = payload.get('pattern') or payload.get('rel_type') or payload.get('signal')
            if pattern:
                similar_patterns = await self.find_similar_patterns(
                    pattern,
                    self.STORAGE_TABLES[output_type],
                    thresholds['similarity']
                )

        # Route based on type-specific logic
        decision = await self._route_by_type(
            output_type, payload, confidence, similar_patterns
        )

        result = ValidationResult(
            valid=(decision in ['direct', 'staged']),
            storage_decision=decision,
            confidence=confidence,
            reason=f"{output_type} routed to {decision}",
            frequency=frequency,
            similar_patterns=similar_patterns,
            metadata={'thresholds': thresholds, 'source': source}
        )

        # Update metrics
        if output_type in self.metrics:
            self.metrics[output_type]['validated'] += 1
            self.metrics[output_type][decision] += 1

        log.info(f"llm_output_validator.validated",
                 output_type=output_type, decision=decision,
                 confidence=confidence, frequency=frequency)

        return result

    def _compute_confidence(self,
                           frequency: int,
                           llm_confidence: float,
                           source: str = 'llm',
                           frequency_threshold: int = 1) -> float:
        """
        Compute unified confidence score across all output types.

        Algorithm:
        - User outputs: always 1.0 (authoritative)
        - LLM outputs: (frequency / threshold) * 0.5 + llm_confidence * 0.5
        - Engine outputs: weighted by source type

        Clamps to [0.0, 1.0].
        """
        if source == 'user':
            return 1.0

        if source == 'engine':
            # Engine-generated confidence gets lower weighting
            return min(1.0, llm_confidence * 0.6)

        # LLM outputs: hybrid of frequency and confidence
        freq_component = min(1.0, frequency / max(frequency_threshold, 1)) * 0.5
        conf_component = llm_confidence * 0.5
        confidence = freq_component + conf_component

        return max(0.0, min(1.0, confidence))

    async def _route_by_type(self,
                            output_type: str,
                            payload: dict,
                            confidence: float,
                            similar_patterns: list[dict]) -> str:
        """
        Route output to storage decision based on type-specific logic.

        Returns: 'direct' | 'staged' | 'rejected' | 'hold'
        """
        if output_type == 'fact':
            # Facts with high confidence → direct
            # Facts with medium confidence → staged
            # Facts with low confidence or invalid structure → rejected
            if confidence >= 0.9:
                return 'direct'
            elif confidence >= 0.6:
                return 'staged'
            else:
                return 'rejected'

        elif output_type == 'retraction_signal':
            # Signals with high confidence → direct
            # Signals with medium confidence → staged
            if confidence >= 0.85:
                return 'direct'
            elif confidence >= 0.6:
                return 'staged'
            else:
                return 'rejected'

        elif output_type == 'entity_type':
            # Entity types similar to existing → staged (for learning)
            # Novel entity types → staged (needs confirmation)
            if similar_patterns:
                return 'staged'
            return 'staged'

        elif output_type == 'rel_type':
            # Novel rel_types → approval/mapping/rejection
            # High similarity → map to existing
            # Frequency >= threshold → approve as new
            if similar_patterns and similar_patterns[0].get('similarity', 0) > 0.85:
                return 'staged'  # Will be mapped in approval phase
            return 'staged'  # Awaits approval

        elif output_type == 'context':
            # Context always → staged (Qdrant only, expires in 30 days)
            return 'staged'

        return 'hold'

    async def find_similar_patterns(self,
                                   pattern: str,
                                   table: str,
                                   similarity_threshold: float = 0.85,
                                   limit: int = 5) -> list[dict]:
        """
        Find existing patterns similar to candidate using semantic similarity.

        Uses nomic-embed-text for embeddings.

        Args:
            pattern: Pattern string (rel_type, signal, entity_type)
            table: Storage table to search ('rel_types', 'retraction_signals', 'entities')
            similarity_threshold: Minimum similarity [0.0, 1.0]
            limit: Max results to return

        Returns: [{'pattern': str, 'similarity': float, 'metadata': dict}, ...]
        """
        if not pattern or not self.db_conn:
            return []

        try:
            # Embed the candidate pattern
            candidate_embedding = await self._embed_text(pattern)
            if not candidate_embedding:
                return []

            # Fetch existing patterns from table
            existing = await self._fetch_patterns(table)
            if not existing:
                return []

            # Compute similarity for each
            similar = []
            for existing_pattern in existing:
                existing_embedding = await self._embed_text(existing_pattern['pattern'])
                if not existing_embedding:
                    continue

                similarity = self._cosine_similarity(candidate_embedding, existing_embedding)
                if similarity >= similarity_threshold:
                    similar.append({
                        'pattern': existing_pattern['pattern'],
                        'similarity': similarity,
                        'metadata': existing_pattern.get('metadata', {})
                    })

            # Sort by similarity (descending) and limit
            similar.sort(key=lambda x: x['similarity'], reverse=True)
            return similar[:limit]

        except Exception as e:
            log.warning(f"llm_output_validator.similarity_search_failed pattern={pattern[:30]}: {e}")
            return []

    async def _embed_text(self, text: str) -> Optional[list[float]]:
        """Embed text using nomic-embed-text via OpenWebUI."""
        try:
            from src.api.llm_client import get_llm_headers

            base_url = self.llm_endpoint.rstrip('/')
            embed_url = os.getenv(
                "EMBEDDING_API_URL",
                base_url
                .replace("/api/chat/completions", "")
                .replace("/v1/chat/completions", "")
                + "/api/embeddings"
            )

            payload = {
                "model": _EMBEDDING_MODEL,
                "input": text,
            }

            response = await httpx.AsyncClient().post(
                embed_url,
                json=payload,
                headers=get_llm_headers(),
                timeout=10.0
            )
            response.raise_for_status()
            data = response.json()

            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["embedding"]

            return None

        except Exception as e:
            log.warning(f"llm_output_validator.embed_failed text={text[:30]}: {e}")
            return None

    async def _fetch_patterns(self, table: str) -> list[dict]:
        """Fetch existing patterns from table for similarity comparison."""
        if not self.db_conn:
            return []

        try:
            if table == 'rel_types':
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT rel_type, label, is_symmetric, is_hierarchy_rel FROM rel_types"
                    )
                    return [
                        {
                            'pattern': row[0],
                            'metadata': {
                                'label': row[1],
                                'is_symmetric': row[2],
                                'is_hierarchy_rel': row[3]
                            }
                        }
                        for row in cur.fetchall()
                    ]

            elif table == 'retraction_signals':
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT signal, signal_category, language FROM retraction_signals"
                    )
                    return [
                        {
                            'pattern': row[0],
                            'metadata': {'category': row[1], 'language': row[2]}
                        }
                        for row in cur.fetchall()
                    ]

            elif table == 'entities':
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT entity_type FROM entities WHERE entity_type != 'unknown'"
                    )
                    return [
                        {'pattern': row[0], 'metadata': {}}
                        for row in cur.fetchall()
                    ]

        except Exception as e:
            log.error(f"llm_output_validator.fetch_patterns_failed table={table}: {e}")

        return []

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def evaluate_batch(self,
                            output_type: str,
                            table: str,
                            batch_size: int = 100) -> dict:
        """
        Batch evaluate pending candidates for approval/promotion.

        Called by re_embedder loop to reconcile pending patterns.

        Args:
            output_type: 'rel_type' | 'retraction_signal' | 'entity_type'
            table: Database table to evaluate from
            batch_size: Max candidates per batch

        Returns: {
            'evaluated': int,
            'approved': int,
            'mapped': int,
            'rejected': int,
            'held': int,
            'results': [decisions]
        }
        """
        results = {
            'evaluated': 0,
            'approved': 0,
            'mapped': 0,
            'rejected': 0,
            'held': 0,
            'results': []
        }

        if not self.db_conn:
            return results

        try:
            # Fetch pending candidates from table
            candidates = await self._fetch_pending_candidates(table, batch_size)

            for candidate in candidates:
                # Evaluate using main validation pipeline
                validation = await self.validate_output(
                    output_type=output_type,
                    payload=candidate,
                    source='engine',
                    llm_confidence=candidate.get('avg_confidence', 0.5),
                    frequency=candidate.get('frequency', 1)
                )

                # Record result
                results['evaluated'] += 1
                action = validation.storage_decision
                if action in results:
                    results[action] += 1
                results['results'].append({
                    'pattern': candidate.get('pattern'),
                    'decision': action,
                    'confidence': validation.confidence,
                    'reason': validation.reason
                })

                log.info(f"llm_output_validator.batch_evaluated",
                         output_type=output_type, pattern=candidate.get('pattern'),
                         decision=action, confidence=validation.confidence)

        except Exception as e:
            log.error(f"llm_output_validator.batch_eval_failed output_type={output_type}: {e}")

        return results

    async def _fetch_pending_candidates(self, table: str, limit: int) -> list[dict]:
        """Fetch pending candidates awaiting evaluation/approval."""
        if not self.db_conn:
            return []

        try:
            if table == 'rel_types':
                with self.db_conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, candidate_rel_type as pattern, occurrence_count as frequency, "
                        "       candidate_subject_type, candidate_object_type "
                        "FROM ontology_evaluations WHERE re_embedder_decision IS NULL "
                        "ORDER BY occurrence_count DESC LIMIT %s",
                        (limit,)
                    )
                    rows = cur.fetchall()
                    return [
                        {
                            'id': row[0],
                            'pattern': row[1],
                            'frequency': row[2],
                            'subject_type': row[3],
                            'object_type': row[4],
                            'avg_confidence': 0.7  # Default for engine-evaluated
                        }
                        for row in rows
                    ]

            # Similar queries for retraction_signals, entity_types, etc.

        except Exception as e:
            log.error(f"llm_output_validator.fetch_pending_failed table={table}: {e}")

        return []

    def get_metrics(self) -> dict:
        """Return global metrics across all output types."""
        return self.metrics

    def reset_metrics(self):
        """Reset all metrics counters."""
        for output_type in self.metrics:
            for key in self.metrics[output_type]:
                self.metrics[output_type][key] = 0
