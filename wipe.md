# FaultLine — Full Wipe & Reset

Destroys all stored facts, entities, aliases, Qdrant vectors, and staged data.
Run this when you want a completely clean slate.

## Prerequisites

All commands assume Docker Compose is running and containers are reachable.
Adjust hostnames if your setup differs:

| Service | Default host |
|---------|-------------|
| PostgreSQL | `localhost:5432` |
| Qdrant | `http://qdrant:6333` |
| FaultLine API | `http://localhost:8001` |

## 1. Wipe PostgreSQL

Connect to the database and truncate all FaultLine tables.
This preserves the schema (tables, indexes, migrations) but removes all rows.

```bash
docker compose exec postgres psql -U faultline -d faultline -c "
  TRUNCATE TABLE
    facts,
    staged_facts,
    entity_attributes,
    entity_aliases,
    entities,
    pending_types,
    staged_fact_confirmations
  CASCADE;
"
```

Or if you prefer a one-liner from the host (adjust user/password):

```bash
PGPASSWORD=faultline psql -h localhost -U faultline -d faultline -c "
  TRUNCATE TABLE
    facts,
    staged_facts,
    entity_attributes,
    entity_aliases,
    entities,
    pending_types,
    staged_fact_confirmations
  CASCADE;
"
```

**Note**: `rel_types` is NOT truncated — the ontology seed data is preserved.

## 2. Wipe Qdrant

Delete all per-user collections. This clears every stored vector.

```bash
# List all FaultLine collections
curl -s http://localhost:6333/collections | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for c in data.get('result', {}).get('collections', []):
    if c['name'].startswith('faultline-'):
        print(c['name'])
"

# Delete each one (replace COLLECTION_NAME with actual names)
curl -X DELETE "http://localhost:6333/collections/COLLECTION_NAME"
```

Or delete all faultline- prefixed collections in one go:

```bash
curl -s http://localhost:6333/collections | \
  python3 -c "
import json, sys, urllib.request
data = json.load(sys.stdin)
for c in data.get('result', {}).get('collections', []):
    name = c['name']
    if name.startswith('faultline-'):
        req = urllib.request.Request(
            f'http://localhost:6333/collections/{name}',
            method='DELETE'
        )
        try:
            urllib.request.urlopen(req)
            print(f'Deleted: {name}')
        except Exception as e:
            print(f'Failed: {name} — {e}')
"
```

The default collection (`faultline-test`) will be recreated automatically by the re-embedder on its next poll cycle.

## 3. Restart FaultLine API (optional)

After wiping, restart the FaultLine backend so it picks up the clean state:

```bash
docker compose restart faultline
```

Or for a full restart:

```bash
docker compose down && docker compose up -d
```

## 4. Verify

```bash
# Check PostgreSQL — all tables should show 0 rows
docker compose exec postgres psql -U faultline -d faultline -c "
  SELECT 'facts' AS tbl, count(*) FROM facts
  UNION ALL SELECT 'staged_facts', count(*) FROM staged_facts
  UNION ALL SELECT 'entities', count(*) FROM entities
  UNION ALL SELECT 'entity_aliases', count(*) FROM entity_aliases
  UNION ALL SELECT 'entity_attributes', count(*) FROM entity_attributes;
"

# Check Qdrant — should show only the default collection with 0 points
curl -s http://localhost:6333/collections/faultline-test | python3 -m json.tool | head -10
```

## Quick script (copy-paste to wipe everything)

```bash
#!/bin/bash
set -e

echo "=== Wiping PostgreSQL ==="
docker compose exec postgres psql -U faultline -d faultline -c "
  TRUNCATE TABLE facts, staged_facts, entity_attributes,
    entity_aliases, entities, pending_types,
    staged_fact_confirmations CASCADE;
"

echo "=== Wiping Qdrant ==="
curl -s http://localhost:6333/collections | python3 -c "
import json, sys, urllib.request
data = json.load(sys.stdin)
for c in data.get('result', {}).get('collections', []):
    name = c['name']
    if name.startswith('faultline-'):
        req = urllib.request.Request(f'http://localhost:6333/collections/{name}', method='DELETE')
        try: urllib.request.urlopen(req); print(f'Deleted: {name}')
        except Exception as e: print(f'Failed: {name} — {e}')
"

echo "=== Restarting FaultLine ==="
docker compose restart faultline

echo "=== Done. Fresh slate. ==="
```
