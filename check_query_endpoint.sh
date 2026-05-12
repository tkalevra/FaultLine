#!/bin/bash
# Quick diagnostic to check if /query endpoint is working and returning facts

FAULTLINE_URL="${FAULTLINE_URL:-http://localhost:8001}"
USER_ID="${USER_ID:-test-user}"

echo "=== /query Endpoint Diagnostics ==="
echo "FAULTLINE_URL: $FAULTLINE_URL"
echo "USER_ID: $USER_ID"
echo ""

# Test 1: Can we reach the endpoint?
echo "[1] Testing endpoint connectivity..."
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  "$FAULTLINE_URL/query" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"tell me about my family\", \"user_id\": \"$USER_ID\"}" \
  --max-time 5)

HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

echo "    HTTP Status: $HTTP_CODE"
if [ "$HTTP_CODE" != "200" ]; then
  echo "    ERROR: /query endpoint returned $HTTP_CODE"
  echo "    Response: $BODY"
  exit 1
fi

echo "    ✓ Endpoint is reachable"
echo ""

# Test 2: Parse response
echo "[2] Checking response structure..."
FACTS_COUNT=$(echo "$BODY" | jq '.facts | length' 2>/dev/null || echo "0")
echo "    Facts returned: $FACTS_COUNT"

if [ "$FACTS_COUNT" -eq 0 ]; then
  echo "    WARNING: No facts returned!"
  echo ""
  echo "[3] Full response for debugging:"
  echo "$BODY" | jq '.' 2>/dev/null || echo "$BODY"
  exit 1
fi

echo "    ✓ Facts are being returned"
echo ""

# Test 3: Check fact types
echo "[3] Checking fact types..."
echo "$BODY" | jq '.facts[] | {subject: .subject, rel_type: .rel_type, object: .object}' 2>/dev/null

echo ""
echo "[4] Checking for spouse facts specifically..."
SPOUSE_FACTS=$(echo "$BODY" | jq '[.facts[] | select(.rel_type == "spouse")] | length' 2>/dev/null || echo "0")
echo "    Spouse facts: $SPOUSE_FACTS"

if [ "$SPOUSE_FACTS" -gt 0 ]; then
  echo "    ✓ Spouse facts found!"
  echo "$BODY" | jq '.facts[] | select(.rel_type == "spouse")'
else
  echo "    WARNING: No spouse facts returned"
fi

echo ""
echo "[5] Checking canonical_identity..."
CANONICAL=$(echo "$BODY" | jq '.canonical_identity' 2>/dev/null)
echo "    Canonical identity: $CANONICAL"

if [ "$CANONICAL" == "null" ]; then
  echo "    ERROR: canonical_identity is null - this prevents graph traversal!"
  echo "    This suggests the database connection or registry initialization failed."
fi

echo ""
echo "=== End Diagnostics ==="
