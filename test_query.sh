#!/bin/bash
# Simple test to check if /query is working correctly
# Run this against the FaultLine API endpoint

set -e

API_URL="${1:-http://localhost:8001}"
USER_ID="${2:-anonymous}"

echo "Testing /query endpoint"
echo "API URL: $API_URL"
echo "User ID: $USER_ID"
echo ""

# Test 1: Simple query
echo "=== TEST 1: POST /query ==="
RESPONSE=$(curl -s -X POST "$API_URL/query" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"tell me about my family\", \"user_id\": \"$USER_ID\"}")

echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"

# Extract key fields
FACTS_COUNT=$(echo "$RESPONSE" | jq '.facts | length' 2>/dev/null || echo "0")
CANONICAL=$(echo "$RESPONSE" | jq '.canonical_identity' 2>/dev/null || echo "null")

echo ""
echo "=== RESULTS ==="
echo "Facts returned: $FACTS_COUNT"
echo "Canonical identity: $CANONICAL"

if [ "$CANONICAL" = "null" ]; then
  echo ""
  echo "[ERROR] canonical_identity is NULL"
  echo "This means the /query endpoint couldn't initialize the database connection or registry."
  echo "Check POSTGRES_DSN environment variable and database connectivity."
  exit 1
fi

if [ "$FACTS_COUNT" = "0" ]; then
  echo ""
  echo "[WARNING] No facts returned"
  echo "Either:"
  echo "  1. No facts in database for this user"
  echo "  2. Graph traversal didn't find any facts"
  echo "  3. All facts were filtered out by relevance scoring"
  exit 1
fi

# Test 2: Check for spouse facts specifically
echo ""
echo "=== TEST 2: Looking for spouse facts ==="
SPOUSE_COUNT=$(echo "$RESPONSE" | jq '[.facts[] | select(.rel_type == "spouse")] | length' 2>/dev/null || echo "0")
echo "Spouse facts: $SPOUSE_COUNT"

if [ "$SPOUSE_COUNT" = "0" ]; then
  echo "[INFO] No spouse facts in response"
  echo "Available fact types:"
  echo "$RESPONSE" | jq -r '.facts[] | .rel_type' 2>/dev/null | sort | uniq -c || true
fi

echo ""
echo "=== SUCCESS ==="
echo "Test completed. Review results above."
