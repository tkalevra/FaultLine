#!/bin/bash
# validate_query.sh — Direct /query API validation
set -euo pipefail

API_URL="${1:-http://localhost:8001}"
USER_ID="${2:-test_local_user}"

echo "=== /query Direct API Validation ==="
echo "API: $API_URL"
echo "User: $USER_ID"
echo

# Test 1: Baseline retrieval — spouse fact
echo "Test 1: Spouse fact retrieval"
RESPONSE=$(curl -s -X POST "$API_URL/query" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"tell me about my family\", \"user_id\": \"$USER_ID\", \"top_k\": 10}")
FACTS=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('facts',[])))" 2>/dev/null || echo "0")
SPOUSE_COUNT=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get('facts',[]) if f.get('rel_type')=='spouse'))" 2>/dev/null || echo "0")

if [ "$SPOUSE_COUNT" -gt 0 ]; then
    echo "✅ PASS: Spouse fact returned (count=$SPOUSE_COUNT)"
    echo "$RESPONSE" | python3 -c "
import json,sys; d=json.load(sys.stdin)
spouse=[f for f in d.get('facts',[]) if f.get('rel_type')=='spouse']
for f in spouse: print(f'  {f[\"subject\"]} -spouse-> {f[\"object\"]}')
"
else
    echo "❌ FAIL: No spouse fact in /query response (total facts=$FACTS)"
fi

# Test 2: Metadata stripping — no user_id in response
echo
echo "Test 2: Metadata stripping (no user_id field)"
USER_ID_LEAK=$(echo "$RESPONSE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for f in d.get('facts',[]) if 'user_id' in f))" 2>/dev/null || echo "0")
if [ "$USER_ID_LEAK" -eq 0 ]; then
    echo "✅ PASS: No user_id field in facts"
else
    echo "❌ FAIL: $USER_ID_LEAK facts have user_id field"
fi

# Test 3: UUID leakage — no raw UUIDs in preferred_names values
echo
echo "Test 3: UUID leakage check"
UUID_LEAK=$(echo "$RESPONSE" | python3 -c "
import json,sys,re
d=json.load(sys.stdin)
uuids=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-')
pn=d.get('preferred_names',{})
leaks=sum(1 for v in pn.values() if uuids.match(str(v)))
# Also check facts subject/object
fact_leaks=sum(1 for f in d.get('facts',[]) if uuids.match(str(f.get('subject',''))) or uuids.match(str(f.get('object',''))))
print(f'{leaks} preferred_names_leaks {fact_leaks} fact_leaks')
" 2>/dev/null || echo "ERROR")
echo "  $UUID_LEAK"

# Test 4: Display name resolution — spouse should resolve
echo
echo "Test 4: Display name resolution"
DISPLAY_RESULT=$(echo "$RESPONSE" | python3 -c "
import json,sys
d=json.load(sys.stdin)
pn=d.get('preferred_names',{})
spouse=[f for f in d.get('facts',[]) if f.get('rel_type')=='spouse']
if not spouse:
    print('NO_SPOUSE_FACT')
else:
    obj=spouse[0].get('object','')
    display=pn.get(obj, obj)
    if display == obj:
        print(f'UNRESOLVED:{obj[:30]}')
    else:
        print(f'RESOLVED:{display}')
" 2>/dev/null || echo "ERROR")
case "$DISPLAY_RESULT" in
    NO_SPOUSE_FACT)
        echo "❌ FAIL: No spouse fact to check resolution"
        ;;
    UNRESOLVED:*)
        echo "❌ FAIL: Spouse UUID maps to itself (UUID→UUID): ${DISPLAY_RESULT#UNRESOLVED:}"
        ;;
    RESOLVED:*)
        echo "✅ PASS: Spouse resolves to '${DISPLAY_RESULT#RESOLVED:}'"
        ;;
    *)
        echo "❌ FAIL: Unexpected result: $DISPLAY_RESULT"
        ;;
esac

# Test 5: Fact count
echo
echo "Test 5: Fact count"
if [ "$FACTS" -gt 0 ]; then
    echo "✅ PASS: $FACTS facts returned"
else
    echo "❌ FAIL: No facts returned"
fi

echo
echo "=== Summary ==="
