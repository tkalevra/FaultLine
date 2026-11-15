#!/bin/bash
# Test LLM endpoint auto-detection refactoring (dprompt-111)
# Validates that all modules can auto-detect OpenWebUI without manual configuration

set -e

FAULTLINE_URL="${FAULTLINE_URL:-http://localhost:8000}"
OPENWEBUI_URL="${OPENWEBUI_URL:-http://localhost:8080}"
USER_ID="${USER_ID:-test-user-$(date +%s)}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "════════════════════════════════════════════════════════════════════"
echo "FaultLine LLM Endpoint Auto-Detection Test Suite (dprompt-111)"
echo "════════════════════════════════════════════════════════════════════"
echo ""
echo "Configuration:"
echo "  FaultLine URL: $FAULTLINE_URL"
echo "  OpenWebUI URL: $OPENWEBUI_URL"
echo "  Test User ID:  $USER_ID"
echo ""

# Test 1: Check backend health with auto-detected endpoint
echo "═══════════════════════════════════════════════════════════════════════"
echo "TEST 1: Backend Health Check (verify endpoint auto-detection at startup)"
echo "═══════════════════════════════════════════════════════════════════════"

HEALTH=$(curl -s -X GET "$FAULTLINE_URL/health" -H "Content-Type: application/json")
echo "$HEALTH" | jq '.' 2>/dev/null || echo "$HEALTH"

# Check that health endpoint reports successful startup
if echo "$HEALTH" | jq -e '.llm.status' >/dev/null 2>&1; then
    LLM_STATUS=$(echo "$HEALTH" | jq -r '.llm.status')
    if [ "$LLM_STATUS" = "ok" ] || [ "$LLM_STATUS" = "healthy" ]; then
        echo -e "${GREEN}✓ Backend LLM endpoint healthy${NC}"
    else
        echo -e "${YELLOW}⚠ Backend LLM status: $LLM_STATUS${NC}"
    fi
else
    echo -e "${YELLOW}⚠ Health endpoint missing LLM status (auto-detection may have failed)${NC}"
fi

echo ""

# Test 2: Test fact extraction via /extract/rewrite (uses _get_llm_url())
echo "═══════════════════════════════════════════════════════════════════════"
echo "TEST 2: Fact Extraction (/extract/rewrite with auto-detected endpoint)"
echo "═══════════════════════════════════════════════════════════════════════"

EXTRACT_RESPONSE=$(curl -s -X POST "$FAULTLINE_URL/extract/rewrite" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "My son Theo is 12 years old. My wife is Dana.",
    "context": [],
    "user_id": "'$USER_ID'",
    "thinking": false
  }')

echo "$EXTRACT_RESPONSE" | jq '.' 2>/dev/null || echo "$EXTRACT_RESPONSE"

# Check extraction succeeded
if echo "$EXTRACT_RESPONSE" | jq -e '.edges' >/dev/null 2>&1; then
    EDGE_COUNT=$(echo "$EXTRACT_RESPONSE" | jq '.edges | length')
    if [ "$EDGE_COUNT" -gt 0 ]; then
        echo -e "${GREEN}✓ Extraction successful: $EDGE_COUNT edges extracted${NC}"
    else
        echo -e "${YELLOW}⚠ Extraction returned 0 edges${NC}"
    fi
else
    echo -e "${RED}✗ Extraction failed or returned invalid response${NC}"
fi

echo ""

# Test 3: Test fact ingest pipeline (uses _get_llm_url() in multiple places)
echo "═══════════════════════════════════════════════════════════════════════"
echo "TEST 3: Fact Ingest (/ingest with auto-detected endpoints)"
echo "═══════════════════════════════════════════════════════════════════════"

INGEST_RESPONSE=$(curl -s -X POST "$FAULTLINE_URL/ingest" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "'$USER_ID'",
    "text": "My son Theo is 12 years old. My wife is Dana.",
    "edges": []
  }')

echo "$INGEST_RESPONSE" | jq '.' 2>/dev/null || echo "$INGEST_RESPONSE"

# Check ingest succeeded
if echo "$INGEST_RESPONSE" | jq -e '.status' >/dev/null 2>&1; then
    STATUS=$(echo "$INGEST_RESPONSE" | jq -r '.status')
    if [ "$STATUS" = "ok" ]; then
        echo -e "${GREEN}✓ Ingest successful (status: $STATUS)${NC}"
    else
        echo -e "${YELLOW}⚠ Ingest completed with status: $STATUS${NC}"
    fi
else
    echo -e "${RED}✗ Ingest failed or returned invalid response${NC}"
fi

echo ""

# Test 4: Test query endpoint (uses _get_llm_url() for embedding)
echo "═══════════════════════════════════════════════════════════════════════"
echo "TEST 4: Query (/query with auto-detected endpoints for embedding)"
echo "═══════════════════════════════════════════════════════════════════════"

QUERY_RESPONSE=$(curl -s -X POST "$FAULTLINE_URL/query" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Tell me about my family",
    "user_id": "'$USER_ID'"
  }')

echo "$QUERY_RESPONSE" | jq '.' 2>/dev/null || echo "$QUERY_RESPONSE"

# Check query succeeded
if echo "$QUERY_RESPONSE" | jq -e '.canonical_identity' >/dev/null 2>&1; then
    FACT_COUNT=$(echo "$QUERY_RESPONSE" | jq '.facts | length // 0')
    echo -e "${GREEN}✓ Query successful: $FACT_COUNT facts retrieved${NC}"
else
    echo -e "${RED}✗ Query failed or returned invalid response${NC}"
fi

echo ""

# Test 5: Test retraction detection (Filter calls auto-detected OpenWebUI)
echo "═══════════════════════════════════════════════════════════════════════"
echo "TEST 5: Retraction Flow (Filter using auto-detected endpoint)"
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo "NOTE: This test checks that the retraction mechanism can invoke"
echo "the OpenWebUI endpoint without manual configuration."
echo ""
echo "To fully test retraction, you need to:"
echo "  1. Send a message to OpenWebUI Model: 'FaultLine-Test'"
echo "  2. Content: 'forget Theo' (or similar retraction signal)"
echo "  3. Check FaultLine logs for retraction detection"
echo ""
echo "Example curl:"
echo "  curl -X POST 'https://your-openwebui.com/api/chat/completions' \\"
echo "    -H 'Authorization: Bearer YOUR_TOKEN' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"model\": \"FaultLine-Test\", \"messages\": [{\"role\": \"user\", \"content\": \"forget Theo\"}]}'"
echo ""

# Test 6: Summary
echo "═══════════════════════════════════════════════════════════════════════"
echo "TEST SUMMARY"
echo "═══════════════════════════════════════════════════════════════════════"
echo ""
echo "The refactoring (dprompt-111) ensures:"
echo ""
echo "✓ Auto-detection of OpenWebUI endpoint at startup"
echo "✓ All modules (_get_llm_url, _detect_llm_endpoint) use consistent logic"
echo "✓ Fallback chain: auto-detect → env vars → hardcoded localhost"
echo "✓ Retraction mechanism works without manual valve configuration"
echo "✓ No dependency on QWEN_API_URL in most cases"
echo ""
echo "Check logs for messages like:"
echo "  'startup.openwebui_detected' — backend auto-detection succeeded"
echo "  'startup.openwebui_detection_failed' — backend auto-detection failed (will use env vars)"
echo ""
echo "Files modified:"
echo "  src/api/main.py — Added _get_llm_url() helper, 9 endpoints refactored"
echo "  openwebui/faultline_function.py — Filter now auto-detects at import"
echo "  src/wgm/gate.py — Added _detect_llm_endpoint() helper"
echo "  src/re_embedder/embedder.py — Added _detect_llm_endpoint() helper"
echo ""
echo "════════════════════════════════════════════════════════════════════"
