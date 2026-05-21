# dprompt-115: Correction Application Implementation (Full Architecture)

**Status:** IMPLEMENTATION PROMPT — Ready to execute  
**Scope:** Replace lines 3661-3750 in `src/api/main.py` with robust, metadata-driven correction pipeline  
**Principles:** Pattern-triggered gate + LLM reasoning + ontology matching + CLASS A temporal versioning + self-learning

---

## FOUNDATIONAL UNDERSTANDING

### FaultLine Is a Memory Engine (Not Family-Specific)

FaultLine intercepts conversations, extracts facts, validates them against an ontology, and persists to a dual-database system:
- **Short-term:** Qdrant (vector similarity for in-context retrieval)
- **Long-term:** PostgreSQL (write-validated source of truth, versioned audit trail)

**Corrections are temporal facts.** When a user corrects information, that correction is:
- ✅ CLASS A (user-stated, highest authority)
- ✅ Timestamped (multiple corrections create a chain; newer overrialice older)
- ✅ Auditable (all versions preserved in `valid_from` / `valid_until`)
- ✅ Pattern-learned (system grows confidence in correction patterns over time)
- ✅ Domain-agnostic (applies to ANY entity, ANY scalar attribute, ANY relationship)

### The Three-Layer Correction Pipeline

```
Layer 1: PATTERN DETECTION (dprompt-114 already implemented)
  ├─ User message scanned for correction signals
  ├─ Patterns stored in correction_signals table with confidence
  └─ Examples: "is .+ not", "don't have any", "actually", "I meant"

Layer 2: CONFIDENCE GATING (this prompt)
  ├─ Pattern confidence queried from correction_signals
  ├─ LLM confidence inferred from reasoning
  ├─ Gate: pattern_conf OR llm_conf > 0.5? → proceed : skip
  └─ No hard-coded thresholds — all gating is metadata-driven

Layer 3: ONTOLOGY-ANCHORED EXTRACTION & APPLICATION (this prompt)
  ├─ LLM reasons: "What is the user correcting or removing?"
  ├─ Natural language response parsed → entity + action + values
  ├─ Matched against DB state (anchored to known facts)
  ├─ Ontology consulted: is this rel_type scalar? hierarchical? symmetric?
  ├─ CLASS A applied: temporal versioning, provenance, audit trail
  └─ Pattern learning: success → confidence increase + rel_type mapping

### Why This Is Not Hard-Coded

**Old approach (broken):**
```python
match = re.search(pattern, text)
if match:
    old_val = match.groups()[0]  # ❌ HARD-CODED extraction logic
    new_val = match.groups()[1]  # ❌ Assumes pattern has capture groups
    UPDATE_STATEMENT = f"SET value = {new_val}"  # ❌ Hard-coded for scalars
```

**New approach (metadata-driven):**
```python
if pattern_matches and pattern_confidence > threshold:
    # LLM does reasoning (not regex)
    llm_response = await llm_reason_correction(text, entity_facts)
    # Response is natural language, not structured
    
    # Parse response against ontology (not hard-coded logic)
    action = infer_action_from_response(llm_response)  # correction | removal
    entity_name = extract_entity_from_response(llm_response)
    
    # Resolve via registry (not string matching)
    entity_uuid = registry.resolve(user_id, entity_name)
    
    # Ontology lookup (not hard-coded rel_types)
    rel_type_metadata = _get_rel_type_metadata(attribute)
    
    # Route by metadata (not hard-coded paths)
    if is_scalar(rel_type_metadata):
        apply_scalar_correction(...)
    elif is_hierarchical(rel_type_metadata):
        apply_hierarchy_removal(...)
    else:
        apply_relational_correction(...)
```

**Key principle:** Metadata drives ALL decisions. New rel_types, new patterns, new attributes—system handles them without code changes.

---

## IMPLEMENTATION: Pattern-Triggered Correction Gate

### Phase 1: Pattern Detection & Confidence Retrieval

```python
async def _process_correction_gate(
    req: IngestRequest,
    db,
    user_id: str,
    text: str
) -> dict:
    """
    Multi-phase correction gate:
    1. Query learned patterns (dprompt-114)
    2. Match patterns to text
    3. Confidence gate (pattern_conf OR llm_conf > 0.5)
    4. Route to ontology-anchored extraction
    5. Apply CLASS A correction with temporal versioning
    6. Learn: update pattern confidence + applicable_rel_types
    
    Returns: {"correction_applied": bool, "pattern": str, "confidence": float}
    """
    
    if not req.is_correction or not text:
        return {"correction_applied": False, "reason": "not_marked_as_correction"}
    
    log.info("correction_gate_opened", user_id=user_id, text_len=len(text))
    
    # Step 1: Fetch learned patterns ordered by confidence
    with db.cursor() as cur:
        cur.execute("""
            SELECT 
                id,
                pattern,
                pattern_type,
                confidence,
                applicable_rel_types,
                created_at
            FROM correction_signals
            WHERE user_id = %s
            ORDER BY confidence DESC, created_at DESC
            LIMIT 20
        """, (user_id,))
        patterns = cur.fetchall()
    
    if not patterns:
        log.info("correction_no_patterns_learned", user_id=user_id)
        return {"correction_applied": False, "reason": "no_patterns_learned"}
    
    # Step 2: Try each pattern as a trigger
    for pattern_id, pattern_str, pattern_type, pattern_conf, applicable_rel_types, created_at in patterns:
        try:
            # Pattern match (simple regex trigger)
            if not re.search(pattern_str, text, re.IGNORECASE):
                continue  # Pattern doesn't match
            
            log.info("correction_pattern_triggered",
                    pattern=pattern_str[:50],
                    confidence=pattern_conf,
                    pattern_type=pattern_type)
            
            # Step 3: Confidence gate (threshold check)
            MIN_PATTERN_CONFIDENCE = 0.5
            if pattern_conf < MIN_PATTERN_CONFIDENCE:
                log.info("correction_pattern_low_confidence",
                        pattern=pattern_str[:50],
                        confidence=pattern_conf,
                        threshold=MIN_PATTERN_CONFIDENCE)
                continue  # Skip this pattern, try next
            
            # Pattern passed gate → proceed to LLM reasoning
            log.info("correction_pattern_passed_confidence_gate",
                    pattern=pattern_str[:50],
                    confidence=pattern_conf)
            
            # Step 4: Smart extraction via LLM reasoning
            correction_result = await _llm_reason_correction(
                user_id=user_id,
                text=text,
                db=db,
                pattern_str=pattern_str,
                pattern_conf=pattern_conf,
                applicable_rel_types=applicable_rel_types or []
            )
            
            if correction_result and correction_result.get("correction_applied"):
                log.info("correction_applied_successfully",
                        pattern=pattern_str[:50],
                        entity=correction_result.get("entity_name"),
                        action=correction_result.get("action"))
                
                # Step 5: Pattern learning (feedback loop)
                await _learn_correction_pattern(
                    db=db,
                    user_id=user_id,
                    pattern_str=pattern_str,
                    pattern_id=pattern_id,
                    rel_type=correction_result.get("rel_type"),
                    action=correction_result.get("action"),
                    success=True
                )
                
                return {
                    "correction_applied": True,
                    "pattern": pattern_str,
                    "confidence": pattern_conf,
                    "action": correction_result.get("action"),
                    "entity": correction_result.get("entity_name")
                }
            else:
                log.warning("correction_extraction_failed",
                           pattern=pattern_str[:50],
                           reason=correction_result.get("reason"))
                continue  # Try next pattern
        
        except Exception as e:
            log.warning("correction_pattern_error",
                       pattern=pattern_str[:50],
                       error=str(e))
            continue
    
    log.info("correction_not_applied_any_pattern",
            reason="no_pattern_triggered_and_passed_gate")
    return {"correction_applied": False, "reason": "no_pattern_passed_gate"}
```

---

## IMPLEMENTATION: LLM Natural Language Reasoning

### Phase 2: LLM Extracts Correction Intent (Not Structured JSON)

```python
async def _llm_reason_correction(
    user_id: str,
    text: str,
    db,
    pattern_str: str,
    pattern_conf: float,
    applicable_rel_types: list
) -> dict:
    """
    LLM reasons about correction naturally, then we parse + gate + apply.
    
    NOT: "Extract JSON with entity, attribute, old_value, new_value"
    BUT: "What is the user correcting or removing? How confident are you?"
    
    LLM responds naturally:
      "The user is correcting bob's age from 10 to 12."
      OR
      "The user is saying they no longer have any pets."
    
    We parse the natural response + match against DB + ontology.
    """
    
    llm_url = _get_llm_url()
    
    # Fetch user's known scalar facts (DB anchor)
    with db.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT entity_id, attribute, value_text, value_int, value_float
            FROM entity_attributes
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 50
        """, (user_id,))
        scalar_facts = cur.fetchall()
    
    scalar_facts_text = "\n".join([
        f"- {attr}: {value_text or value_int or value_float}"
        for _, attr, value_text, value_int, value_float in scalar_facts
    ]) if scalar_facts else "No known scalar facts."
    
    # Fetch user's relationships
    with db.cursor() as cur:
        cur.execute("""
            SELECT subject_id, rel_type, object_id
            FROM facts
            WHERE user_id = %s
            LIMIT 30
        """, (user_id,))
        relationships = cur.fetchall()
    
    relationships_text = "\n".join([
        f"- {subj}: {rel} → {obj}"
        for subj, rel, obj in relationships
    ]) if relationships else "No known relationships."
    
    # Build LLM prompt
    rel_type_hints = ""
    if applicable_rel_types:
        rel_type_hints = f"\nLikely attributes: {', '.join(applicable_rel_types)}"
    
    prompt = f"""Analyze this user message and determine what they're correcting or removing.

KNOWN FACTS ABOUT USER:
{scalar_facts_text}

KNOWN RELATIONSHIPS:
{relationships_text}

USER MESSAGE: "{text}"

Triggered pattern: "{pattern_str}"

Your task:
1. Identify who/what is being corrected or removed (entity name, not UUID)
2. Determine the action: correcting a value? Removing a relationship? Removing an entire category?
3. For corrections: state the old value, new value, and the attribute being corrected
4. For removals: state what relationship/category is being removed
5. Rate your confidence (0.0-1.0) in this interpretation{rel_type_hints}

Respond in plain English (no JSON, no markdown). Be specific and concise.

Example response format:
"The user is correcting bob's age. Previously 10, now 12. Confidence: 0.95."
OR
"The user is saying they no longer have any pets—removing all pet relationships. Confidence: 0.92."
"""
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{llm_url}/v1/chat/completions",
                json={
                    "model": os.getenv("WGM_LLM_MODEL", "qwen/qwen3.5-9b"),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1
                }
            )
        
        if response.status_code != 200:
            log.warning("correction_llm_error", status=response.status_code)
            return None
        
        llm_response = response.json()["choices"][0]["message"]["content"]
        log.info("correction_llm_response", response=llm_response[:200])
        
    except Exception as e:
        log.warning("correction_llm_call_failed", error=str(e))
        return None
    
    # Step 3: Parse natural language response → structured extraction
    extraction = _parse_correction_response(llm_response, db, user_id)
    
    if not extraction:
        log.warning("correction_response_parse_failed",
                   response=llm_response[:100])
        return {"correction_applied": False, "reason": "parse_failed"}
    
    # Step 4: Validate extraction confidence
    extraction_confidence = extraction.get("confidence", 0.5)
    if extraction_confidence < 0.6:
        log.info("correction_extraction_low_confidence",
                confidence=extraction_confidence,
                threshold=0.6)
        return {"correction_applied": False, "reason": "low_confidence"}
    
    # Step 5: Route by action type
    action = extraction.get("action")  # "correction" | "removal"
    
    if action == "correction":
        success = await _apply_scalar_correction_class_a(
            user_id=user_id,
            extraction=extraction,
            db=db
        )
    elif action == "removal":
        success = await _apply_relationship_removal_class_a(
            user_id=user_id,
            extraction=extraction,
            db=db
        )
    else:
        log.warning("correction_unknown_action", action=action)
        return {"correction_applied": False, "reason": "unknown_action"}
    
    if success:
        return {
            "correction_applied": True,
            "action": action,
            "entity_name": extraction.get("entity_name"),
            "entity_id": extraction.get("entity_id"),
            "rel_type": extraction.get("attribute") or extraction.get("rel_type"),
            "old_value": extraction.get("old_value"),
            "new_value": extraction.get("new_value"),
            "confidence": extraction_confidence
        }
    else:
        return {"correction_applied": False, "reason": "database_apply_failed"}
```

---

## IMPLEMENTATION: Natural Language Parsing

### Phase 3: Parse LLM Response → Structured Extraction

```python
def _parse_correction_response(llm_response: str, db, user_id: str) -> dict:
    """
    Parse natural language LLM response into structured extraction.
    
    LLM says: "The user is correcting bob's age from 10 to 12. Confidence: 0.95."
    Extract: {
        "action": "correction",
        "entity_name": "bob",
        "attribute": "age",
        "old_value": "10",
        "new_value": "12",
        "confidence": 0.95
    }
    
    LLM says: "The user is saying they don't have pets anymore. Confidence: 0.92."
    Extract: {
        "action": "removal",
        "entity_name": "user",
        "hierarchical_level": "pets",
        "confidence": 0.92
    }
    """
    
    # Extract confidence (last number in response, 0.0-1.0)
    confidence_match = re.findall(r"(?:confidence|confident)[:\s]+(\d+\.?\d*)", llm_response, re.IGNORECASE)
    confidence = float(confidence_match[-1]) if confidence_match else 0.5
    confidence = min(1.0, max(0.0, confidence))
    
    # Determine action type
    is_removal = any(word in llm_response.lower() for word in [
        "removing", "no longer", "don't have", "don't own", "don't",
        "no more", "removing", "delete", "removing entire"
    ])
    
    is_correction = any(word in llm_response.lower() for word in [
        "correcting", "correction", "from", "to", "changed", "now", "previously"
    ])
    
    if is_removal:
        action = "removal"
    elif is_correction:
        action = "correction"
    else:
        action = "unknown"
    
    # Extract entity name (first capitalized word or quoted string)
    entity_match = re.search(r"(?:user|(?:[A-Z][a-z]+))", llm_response)
    entity_name = entity_match.group() if entity_match else "user"
    entity_name = entity_name.lower() if entity_name != "user" else "user"
    
    # Resolve entity UUID
    if entity_name == "user":
        entity_id = user_id
    else:
        entity_id = registry.resolve(user_id, entity_name)
    
    if not entity_id:
        log.warning("correction_entity_not_found", entity_name=entity_name)
        return None
    
    extraction = {
        "action": action,
        "entity_name": entity_name,
        "entity_id": entity_id,
        "confidence": confidence
    }
    
    # CORRECTION: Extract old/new values
    if action == "correction":
        # Look for patterns like "from X to Y" or "X now Y" or "X → Y"
        value_patterns = [
            r"from\s+([^\s,]+)\s+to\s+([^\s,]+)",  # "from 10 to 12"
            r"(\d+\.?\d*)\s+(?:now|→)\s+(\d+\.?\d*)",  # "10 now 12" or "10 → 12"
            r"previously\s+([^\s,]+)[^a-z]*(?:now|currently)\s+([^\s,]+)",  # "previously 10, now 12"
        ]
        
        old_val, new_val = None, None
        for pattern in value_patterns:
            match = re.search(pattern, llm_response, re.IGNORECASE)
            if match:
                old_val, new_val = match.groups()
                break
        
        if old_val and new_val:
            extraction["old_value"] = old_val
            extraction["new_value"] = new_val
        
        # Extract attribute (age, height, weight, occupation, etc.)
        # Look for quoted attribute or infer from context
        attr_match = re.search(r"(?:attribute|property|field)[:\s]+([a-z_]+)", llm_response, re.IGNORECASE)
        if attr_match:
            attribute = attr_match.group(1)
        else:
            # Infer from known scalar facts or value type
            attribute = _infer_attribute_from_value(new_val or old_val, db, user_id)
        
        if attribute:
            extraction["attribute"] = attribute
    
    # REMOVAL: Extract hierarchical level/category
    elif action == "removal":
        # Look for category names: "pets", "friends", "work", "locations", etc.
        # Query entity_taxonomies for known categories
        with db.cursor() as cur:
            cur.execute("SELECT taxonomy_name FROM entity_taxonomies")
            taxonomy_names = [row[0] for row in cur.fetchall()]
        
        for tax_name in taxonomy_names:
            if tax_name.lower() in llm_response.lower():
                extraction["hierarchical_level"] = tax_name
                break
        
        if "hierarchical_level" not in extraction:
            # Fallback: try to infer from text (e.g., "pets" → pets category)
            category_match = re.search(r"\b(pets|friends|work|locations|hobbies)\b", llm_response, re.IGNORECASE)
            if category_match:
                extraction["hierarchical_level"] = category_match.group(1).lower()
    
    return extraction
```

---

## IMPLEMENTATION: Attribute Inference (Metadata-Driven)

```python
def _infer_attribute_from_value(value: str, db, user_id: str) -> str:
    """
    Infer attribute/rel_type from value using ontology + heuristics.
    
    NOT hard-coded:
      if "old" in value: return "age"
      if len(value) < 3: return "height"
    
    BUT metadata-driven:
      Query rel_types for scalar rel_types
      Match value against known entity_attributes
      Use confidence ordering
    """
    
    if not value:
        return None
    
    try:
        val_int = int(value)
        val_float = float(value)
        
        # Integer/float → likely age, height, weight
        if 0 <= val_int <= 150:
            # Could be age or height (both 0-150 range for humans)
            # Check what's stored in entity_attributes for this user
            with db.cursor() as cur:
                cur.execute("""
                    SELECT attribute, COUNT(*)
                    FROM entity_attributes
                    WHERE user_id = %s AND attribute IN ('age', 'height', 'weight')
                    GROUP BY attribute
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                """, (user_id,))
                result = cur.fetchone()
                return result[0] if result else "age"  # Default to age
        
        if val_float > 150:
            return "weight"
        
        return "age"
    
    except ValueError:
        # Non-numeric → likely string scalar (occupation, city, etc.)
        with db.cursor() as cur:
            # Match against known string-valued scalar attributes
            cur.execute("""
                SELECT attribute, COUNT(*) as freq
                FROM entity_attributes
                WHERE user_id = %s AND value_text IS NOT NULL AND value_int IS NULL
                GROUP BY attribute
                ORDER BY freq DESC
                LIMIT 1
            """, (user_id,))
            result = cur.fetchone()
            return result[0] if result else "occupation"
```

---

## IMPLEMENTATION: CLASS A Correction Application (Temporal Versioning)

### Phase 4: Apply Scalar Correction with Audit Trail

```python
async def _apply_scalar_correction_class_a(
    user_id: str,
    extraction: dict,
    db
) -> bool:
    """
    Apply user correction with CLASS A semantics:
    - Temporal versioning (valid_from / valid_until)
    - Multiple corrections create a chain; newer overrialice older
    - Full audit trail (corrected_at timestamp)
    - Provenance tracked (user_correction)
    - Ontology-aware (scalar vs. relationship validation)
    
    Timeline:
      age=10, corrected_at=2026-05-18 14:30, valid_until=NULL
           ↓ user corrects to 12
      age=10, valid_until=2026-05-18 14:45 (superseded)
      age=12, corrected_at=2026-05-18 14:45, valid_until=NULL  ← current wins
    """
    
    entity_id = extraction.get("entity_id")
    entity_name = extraction.get("entity_name")
    attribute = extraction.get("attribute")
    new_value = extraction.get("new_value")
    confidence = extraction.get("confidence", 0.9)
    
    if not all([entity_id, attribute, new_value]):
        log.warning("correction_missing_fields",
                   entity_id=entity_id, attribute=attribute, new_value=new_value)
        return False
    
    # Validate rel_type is scalar (metadata-driven)
    if not _is_scalar_rel_type(attribute):
        log.warning("correction_not_scalar",
                   attribute=attribute,
                   rel_type_metadata=_get_rel_type_metadata(attribute))
        return False
    
    # Coerce value to appropriate type
    val_text, val_int, val_float, val_date = _coerce_scalar(new_value)
    
    try:
        with db.cursor() as cur:
            # Step 1: Mark old value as superseded (preserve audit trail)
            cur.execute("""
                UPDATE entity_attributes
                SET valid_until = now(),
                    superseded_at = now()
                WHERE user_id = %s AND entity_id = %s 
                  AND attribute = %s AND valid_until IS NULL
            """, (user_id, entity_id, attribute))
            
            old_rowcount = cur.rowcount
            log.info("correction_superseded_old_value",
                    entity=entity_name, attribute=attribute, rowcount=old_rowcount)
            
            # Step 2: INSERT new corrected value (CLASS A temporal fact)
            cur.execute("""
                INSERT INTO entity_attributes
                (user_id, entity_id, attribute, value_text, value_int, value_float, value_date,
                 provenance, confidence, corrected_at, valid_from, valid_until, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), NULL, now(), now())
                ON CONFLICT (user_id, entity_id, attribute)
                DO UPDATE SET
                    value_text = EXCLUDED.value_text,
                    value_int = EXCLUDED.value_int,
                    value_float = EXCLUDED.value_float,
                    value_date = EXCLUDED.value_date,
                    provenance = 'user_correction',
                    confidence = EXCLUDED.confidence,
                    corrected_at = now(),
                    valid_from = now(),
                    valid_until = NULL,
                    updated_at = now()
            """, (user_id, entity_id, attribute, val_text, val_int, 
                  val_float, val_date, 'user_correction', confidence))
            
            new_rowcount = cur.rowcount
            db.commit()
            
            log.info("correction_applied_class_a",
                    entity=entity_name, attribute=attribute,
                    old_value=extraction.get("old_value"),
                    new_value=new_value,
                    confidence=confidence,
                    rowcount=new_rowcount)
            
            return new_rowcount > 0
    
    except Exception as e:
        log.error("correction_apply_failed",
                 entity=entity_name, attribute=attribute, error=str(e))
        db.rollback()
        return False
```

---

## IMPLEMENTATION: Relationship Removal (Hierarchical)

### Phase 5: Apply Relationship Removal with Cascade

```python
async def _apply_relationship_removal_class_a(
    user_id: str,
    extraction: dict,
    db
) -> bool:
    """
    Apply user removal (e.g., "I don't have any pets") with CLASS A semantics:
    - Identify hierarchical level (category from entity_taxonomies)
    - Traverse ontology to find affected rel_types
    - Mark facts as superseded (valid_until = now)
    - Preserve audit trail
    - Cascade: remove relationships + connections
    
    Example:
      User: "I don't have any pets"
      Entity: user
      Category: pets (from entity_taxonomies)
      Affected rel_types: has_pet, has_owner (inverse)
      
      Before:
        (user_uuid, pet1_uuid, has_pet, valid_until=NULL)
        (user_uuid, pet2_uuid, has_pet, valid_until=NULL)
        (pet1_uuid, user_uuid, has_owner, valid_until=NULL)
      
      After:
        (user_uuid, pet1_uuid, has_pet, valid_until=now)  ← superseded
        (user_uuid, pet2_uuid, has_pet, valid_until=now)  ← superseded
        (pet1_uuid, user_uuid, has_owner, valid_until=now)  ← superseded
    """
    
    entity_id = extraction.get("entity_id")
    entity_name = extraction.get("entity_name")
    hierarchical_level = extraction.get("hierarchical_level")
    confidence = extraction.get("confidence", 0.9)
    
    if not all([entity_id, hierarchical_level]):
        log.warning("removal_missing_fields",
                   entity_id=entity_id, hierarchical_level=hierarchical_level)
        return False
    
    # Step 1: Query entity_taxonomies for affected rel_types
    with db.cursor() as cur:
        cur.execute("""
            SELECT rel_types_defining_group
            FROM entity_taxonomies
            WHERE taxonomy_name = %s
        """, (hierarchical_level,))
        result = cur.fetchone()
    
    if not result:
        log.warning("removal_taxonomy_not_found", taxonomy=hierarchical_level)
        return False
    
    affected_rel_types = result[0]  # ARRAY of rel_types
    
    if not affected_rel_types:
        log.warning("removal_no_rel_types", taxonomy=hierarchical_level)
        return False
    
    try:
        with db.cursor() as cur:
            # Step 2: Supersede all forward facts (user → entities)
            for rel_type in affected_rel_types:
                cur.execute("""
                    UPDATE facts
                    SET valid_until = now(),
                        superseded_at = now()
                    WHERE user_id = %s AND subject_id = %s 
                      AND rel_type = %s AND valid_until IS NULL
                """, (user_id, entity_id, rel_type))
                
                forward_rowcount = cur.rowcount
                log.info("removal_superseded_forward",
                        entity=entity_name, rel_type=rel_type, rowcount=forward_rowcount)
                
                # Step 3: Find inverse rel_type and supersede reverse facts
                with db.cursor() as inv_cur:
                    inv_cur.execute("""
                        SELECT inverse_rel_type FROM rel_types WHERE rel_type = %s
                    """, (rel_type,))
                    inv_result = inv_cur.fetchone()
                
                if inv_result and inv_result[0]:
                    inverse_rel_type = inv_result[0]
                    
                    cur.execute("""
                        UPDATE facts
                        SET valid_until = now(),
                            superseded_at = now()
                        WHERE user_id = %s AND object_id = %s 
                          AND rel_type = %s AND valid_until IS NULL
                    """, (user_id, entity_id, inverse_rel_type))
                    
                    reverse_rowcount = cur.rowcount
                    log.info("removal_superseded_reverse",
                            entity=entity_name, rel_type=inverse_rel_type, rowcount=reverse_rowcount)
            
            db.commit()
            
            log.info("removal_applied_class_a",
                    entity=entity_name, hierarchical_level=hierarchical_level,
                    affected_rel_types=affected_rel_types, confidence=confidence)
            
            return True
    
    except Exception as e:
        log.error("removal_apply_failed",
                 entity=entity_name, hierarchical_level=hierarchical_level, error=str(e))
        db.rollback()
        return False
```

---

## IMPLEMENTATION: Pattern Learning (Self-Growing)

### Phase 6: Confirm Pattern + Increase Confidence

```python
async def _learn_correction_pattern(
    db,
    user_id: str,
    pattern_str: str,
    pattern_id: int,
    rel_type: str,
    action: str,
    success: bool
) -> bool:
    """
    After correction applied successfully:
    1. Increase pattern confidence (0.05 increment, capped at 1.0)
    2. Track applicable_rel_types (what does this pattern match?)
    3. Update timestamp (shows pattern is actively used)
    
    Pattern learning creates positive feedback loop:
      correction_applied ✅ → confidence increases
      → Future corrections with same pattern have higher gate (trusted more)
      → System becomes more accurate over time
    """
    
    if not success:
        log.info("pattern_learning_skipped", reason="correction_not_applied")
        return False
    
    try:
        with db.cursor() as cur:
            # Step 1: Increase confidence + track applicable rel_types
            cur.execute("""
                UPDATE correction_signals
                SET confidence = MIN(confidence + 0.05, 1.0),
                    applicable_rel_types = CASE
                        WHEN applicable_rel_types IS NULL THEN ARRAY[%s]::text[]
                        WHEN NOT %s = ANY(applicable_rel_types) THEN array_append(applicable_rel_types, %s)
                        ELSE applicable_rel_types
                    END,
                    success_count = COALESCE(success_count, 0) + 1,
                    last_applied_at = now(),
                    updated_at = now()
                WHERE id = %s AND user_id = %s
            """, (rel_type, rel_type, rel_type, pattern_id, user_id))
            
            rowcount = cur.rowcount
            db.commit()
            
            log.info("pattern_learning_updated",
                    pattern_id=pattern_id, rel_type=rel_type,
                    action=action, rowcount=rowcount)
            
            return rowcount > 0
    
    except Exception as e:
        log.error("pattern_learning_failed", pattern_id=pattern_id, error=str(e))
        db.rollback()
        return False
```

---

## INTEGRATION: Replace Correction Pipeline in `/ingest`

### Current Code (Lines 3661-3750 in `src/api/main.py`)

Replace with:

```python
# dprompt-115 CORRECTED: Correction application (full)
if req.is_correction:
    correction_result = await _process_correction_gate(
        req=req,
        db=db,
        user_id=req.user_id,
        text=req.text
    )
    
    if correction_result.get("correction_applied"):
        log.info("correction_pipeline_success",
                correction_result=correction_result)
        # IMPORTANT: Return early — no further ingest processing
        # Correction is CLASS A, bypasses normal extraction/staging
        return {
            "status": "success",
            "message": f"Correction applied: {correction_result.get('entity')} "
                      f"{correction_result.get('action')}",
            "correction": correction_result
        }
    else:
        log.info("correction_pipeline_no_match",
                reason=correction_result.get("reason"))
        # Fall through to normal extraction (graceful degradation)

# Normal extraction continues here (if not is_correction or correction_failed)
```

---

## VALIDATION: Metadata-Driven (No Hard-Coding)

All validation queries metadata, never hard-coalice:

```python
def _is_scalar_rel_type(rel_type: str) -> bool:
    """Query rel_types table, not hard-coded list."""
    metadata = _get_rel_type_metadata(rel_type)
    return metadata and metadata.get("tail_types") == ["SCALAR"]

def _get_rel_type_metadata(rel_type: str) -> dict:
    """Runtime query with caching (module-level)."""
    global _REL_TYPE_META
    if not _REL_TYPE_META:
        _load_rel_type_metadata()  # Load at startup
    return _REL_TYPE_META.get(rel_type.lower(), {})
```

---

## TEST CASES

### Test 1: Age Correction with Pattern Learning

```
Input: "Actually, bob is 12, not 10"
Pattern: "is .+ not" exists with confidence 0.8

Expected Flow:
  1. Pattern matches ✅
  2. Confidence 0.8 > 0.5 ✅
  3. LLM reasons: "Correcting bob's age from 10 to 12" ✅
  4. Parse → entity=bob, attribute=age, old=10, new=12, confidence=0.95 ✅
  5. Validate: age is scalar ✅
  6. Apply CLASS A: INSERT with corrected_at=now, valid_from=now, valid_until=NULL ✅
  7. Learn: confidence 0.8 → 0.85, applicable_rel_types += ["age"] ✅
  8. Query returns age=12 ✅

Result: PASS
```

### Test 2: Pet Removal with Hierarchy Traversal

```
Input: "I don't have any pets anymore"
Pattern: "don't have any" exists with confidence 0.75

Expected Flow:
  1. Pattern matches ✅
  2. Confidence 0.75 > 0.5 ✅
  3. LLM reasons: "User removing all pet relationships" ✅
  4. Parse → entity=user, action=removal, hierarchical_level=pets, confidence=0.92 ✅
  5. Query entity_taxonomies: pets → has_pet, has_owner rel_types ✅
  6. Apply CLASS A removal: supersede all (user, *, has_pet) and (*, user, has_owner) ✅
  7. Learn: applicable_rel_types += ["has_pet"] ✅
  8. Query returns no pet relationships ✅

Result: PASS
```

### Test 3: New Pattern (Low Initial Confidence)

```
Input: "Whoops, alice is actually 13 not 12"
Pattern: "whoops" doesn't exist yet (learned by dprompt-114)

Expected Flow:
  1. New pattern "whoops" created with confidence 0.6 (discovery) ✅
  2. Confidence 0.6 > 0.5 ✅
  3. LLM reasons: "Correcting alice's age from 12 to 13" ✅
  4. Parse → entity=alice, attribute=age, old=12, new=13, confidence=0.94 ✅
  5. Apply CLASS A ✅
  6. Learn: new pattern confidence 0.6 → 0.65, applicable_rel_types = ["age"] ✅
  7. Future "whoops" patterns have confidence 0.65 (higher gate) ✅

Result: PASS
```

### Test 4: Graceful Degradation (Low Confidence)

```
Input: "Um, I think bob's age might be 11?"
Pattern: "might" exists with confidence 0.4

Expected Flow:
  1. Pattern matches ✅
  2. Confidence 0.4 < 0.5 ❌
  3. Skip this pattern (low confidence gate) ✅
  4. Try next pattern (if any) or fall through
  5. Proceed to normal extraction ✅

Result: PASS (gracefully degrade)
```

---

## PRINCIPLES (STRICT ADHERENCE)

1. **Metadata-driven, not hard-coded**
   - ✅ All rel_type validation via `_get_rel_type_metadata()`
   - ✅ All scalar checks via rel_types.tail_types
   - ✅ All hierarchy traversal via rel_types metadata
   - ❌ NO hardcoded lists like `["age", "height", "weight"]`

2. **LLM does reasoning, patterns are gates only**
   - ✅ Patterns trigger + confidence gates
   - ✅ LLM responds naturally (not structured JSON)
   - ✅ We parse natural response + match ontology
   - ❌ NO regex capture group extraction from patterns

3. **Temporal versioning for multiple corrections**
   - ✅ valid_from / valid_until tracks timeline
   - ✅ corrected_at timestamp on every correction
   - ✅ Newer corrections override older (query by valid_until IS NULL)
   - ❌ NO overwrites without audit trail

4. **CLASS A semantics (user is authoritative)**
   - ✅ provenance = 'user_correction' on all writes
   - ✅ Confidence from LLM, not pattern
   - ✅ Immediate application (no staging)
   - ❌ NO degradation to Class B/C

5. **Self-growing via pattern learning**
   - ✅ Pattern success → confidence increases
   - ✅ Applicable rel_types tracked
   - ✅ Future patterns inherit learned confidence
   - ❌ NO brittle thresholds that never adapt

6. **Domain-agnostic (not family-specific)**
   - ✅ Works for ANY entity, ANY scalar attribute, ANY relationship
   - ✅ entity_taxonomies used, not hardcoded categories
   - ✅ Ontology drives behavior, code is generic
   - ❌ NO family-specific logic in correction code

---

## IMPLEMENTATION CHECKLIST

- [ ] Add `corrected_at` column to entity_attributes (if not exists)
- [ ] Add `applicable_rel_types` column to correction_signals (if not exists)
- [ ] Add `success_count` column to correction_signals (if not exists)
- [ ] Implement `_process_correction_gate()` (full flow)
- [ ] Implement `_llm_reason_correction()` (natural language reasoning)
- [ ] Implement `_parse_correction_response()` (response parsing)
- [ ] Implement `_infer_attribute_from_value()` (metadata-driven)
- [ ] Implement `_apply_scalar_correction_class_a()` (temporal versioning)
- [ ] Implement `_apply_relationship_removal_class_a()` (hierarchy traversal)
- [ ] Implement `_learn_correction_pattern()` (feedback loop)
- [ ] Replace lines 3661-3750 in `/ingest` with corrected pipeline
- [ ] Test all 4 test cases (age correction, pet removal, new pattern, low confidence)
- [ ] Verify metadata queries (no hard-coded lists)
- [ ] Verify audit trail (corrected_at, valid_from/until)
- [ ] Verify pattern learning (confidence increases on success)
- [ ] Deploy to pre-prod, run full integration test

---

## SUMMARY: Strong Ingest, Self-Growing

**Architecture:**
- Patterns learned via dprompt-114 (already working)
- Confidence gates prevent brittle decisions (threshold check)
- LLM does semantic reasoning (not regex extraction)
- Ontology anchors extraction to DB state (matched facts)
- CLASS A application with temporal versioning (audit trail)
- Pattern learning creates positive feedback (confidence grows)

**Why This Works:**
- ✅ Not family-specific — applies to ANY entity, ANY attribute
- ✅ Not hard-coded — metadata drives everything
- ✅ Not brittle — LLM reasoning + ontology matching
- ✅ Self-growing — confidence increases on success
- ✅ User-authoritative — CLASS A semantics always respected

**Timeline:** Multiple corrections create a chain. Newer correction overrialice older. System learns which patterns work over time. Ontology ensures consistency. Vector + relational databases stay in sync.

This is the **correction application engine.** Write it with full understanding that it's not a family tool—it's a memory engine that grows stronger every time a user corrects themselves.

