#!/usr/bin/env python3
import os
import psycopg2

dsn = os.environ.get('POSTGRES_DSN', 'postgresql://postgres@localhost:5432/faultline')
db = psycopg2.connect(dsn)
cur = db.cursor()

print('=== Checking facts table for string entity_ids ===')
cur.execute("""
SELECT DISTINCT subject_id FROM facts
WHERE user_id = 'anonymous'
AND subject_id NOT LIKE '%-%-%-%-'
LIMIT 20
""")
string_subjects = cur.fetchall()
print(f'String subjects found: {len(string_subjects)}')
for row in string_subjects[:5]:
    print(f'  - {row[0]}')

cur.execute("""
SELECT DISTINCT object_id FROM facts
WHERE user_id = 'anonymous'
AND object_id NOT LIKE '%-%-%-%-'
LIMIT 20
""")
string_objects = cur.fetchall()
print(f'String objects found: {len(string_objects)}')
for row in string_objects[:5]:
    print(f'  - {row[0]}')

# Check staged_facts
print('\n=== Checking staged_facts table for string entity_ids ===')
cur.execute("""
SELECT DISTINCT subject_id FROM staged_facts
WHERE user_id = 'anonymous'
AND subject_id NOT LIKE '%-%-%-%-'
LIMIT 20
""")
staged_subjects = cur.fetchall()
print(f'String subjects in staged_facts: {len(staged_subjects)}')
for row in staged_subjects[:5]:
    print(f'  - {row[0]}')

cur.execute("""
SELECT DISTINCT object_id FROM staged_facts
WHERE user_id = 'anonymous'
AND object_id NOT LIKE '%-%-%-%-'
LIMIT 20
""")
staged_objects = cur.fetchall()
print(f'String objects in staged_facts: {len(staged_objects)}')
for row in staged_objects[:5]:
    print(f'  - {row[0]}')

db.close()
