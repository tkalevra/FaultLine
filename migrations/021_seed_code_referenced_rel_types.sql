-- Migration 021 (seed order repair, 2026-09-16 — gauntlet seed-030-first-boot):
-- seed the three rel_types the corpus ASSUMES but never INSERTS: member_of, has_os, ip_address.
--
-- THE WOUND (measured on a fresh database, one migration pass, boot ledger read back):
--   migrations/030_rel_type_aliases.sql seeds 75 alias rows (73 distinct aliases) in ONE multi-row INSERT carrying an FK
--   to rel_types(rel_type). Two of those rows name `member_of`, which NO migration inserts —
--   005/006/007/016/017/025 seed everything else 030 references, `member_of` is only ever
--   UPDATEd (022, 024, 030_strengthen, 031, 081, 089, 096). PostgreSQL rejects the WHOLE
--   statement (SQLSTATE 23503 foreign_key_violation — a multi-row VALUES is one statement, and a
--   statement either succeeds or fails as a unit: "Key (canonical_rel_type)=(member_of) is not
--   present in table rel_types"), so ZERO of the 75 alias rows land, the boot ledger records
--   030_rel_type_aliases as `failed`, and the file only applies on boot #2 — after
--   src/api/main.py::_ensure_schema has minted member_of/has_os/ip_address into public at app
--   start. Every single-pass database (throwaway, CI, a fresh deployment's first boot) and every
--   tenant provisioned before that second boot therefore carries none of the P26 synonym set
--   (married_to / spouse_of / husband_of / wife_of -> spouse) nor the other ~70 aliases.
--
-- WHY A MIGRATION AND NOT A ROW-WISE 030: the rows BELONG in the seed. Migration 024's own
--   comment lists "member_of, is_a, has_ip, has_os, hostname, fqdn, ip_address, located_at -> C"
--   as the seeded Class-C set, 019 puts member_of in a taxonomy's defining rels, and the WGM
--   ontology table documents member_of as a core rel. Making 030 skip rows with a NOTICE would
--   silently accept a broken seed and leave runtime code as the owner. One owner per seeded rel:
--   this file. `_ensure_schema` keeps its ON CONFLICT DO NOTHING net (fail-safe) but now logs
--   CRIT if it ever has to insert, because that means the seed has a gap again.
--
-- WHY 021 (before 022): the metadata UPDATEs in 022 (is_hierarchy_rel, allows_leaf_rels),
--   024 (storage_target/fact_class), 031 (natural_language), 081 (2p template), 096
--   (temporal_class) are stamped `applied` on the first boot by the ledger and never re-run, so
--   a row minted AFTER them at app start never receives them (measured: a twice-booted fresh
--   box has member_of.natural_language = NULL). Seeding before 022 gives a first boot the same
--   row every pre-ledger box got from re-running the corpus on every start.
--
-- VALUES mirror `_ensure_schema`'s `_MISSING_TYPES` exactly (label / category /
--   correction_behavior / source='builtin'); no head/tail types are invented here — on every
--   deployed box these three rows carry NULL types, and typing them is an ontology decision,
--   not a seed-order repair. Columns used all exist by 017 (source/correction_behavior: 007;
--   category: 013). ON CONFLICT DO NOTHING: an existing row (any box that already booted twice)
--   is never rewritten — seed never overrides a live row (authority order).
INSERT INTO public.rel_types (rel_type, label, category, correction_behavior, source, engine_generated, confidence)
VALUES
    ('member_of',  'Member Of',            'identity', 'supersede', 'builtin', false, 1.0),
    ('has_os',     'Has Operating System', 'system',   'supersede', 'builtin', false, 1.0),
    ('ip_address', 'IP Address',           'system',   'supersede', 'builtin', false, 1.0)
ON CONFLICT (rel_type) DO NOTHING;
