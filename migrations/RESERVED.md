# Reserved migration numbers

These numbers are used by migrations that exist only on the public FOSS line
(tkalevra/FaultLine main/es/it). Do not reuse them on saas; skip to the next
free number so a later port in either direction never shares a prefix.

| Number | FOSS migration | Purpose |
|---|---|---|
| 282 | 282_linguistic_cues_spanish_engine_v2.sql / 282_linguistic_cues_italian_engine.sql | es/it FOSS cue classes (the es and it lines THEMSELVES collide at 282 - different files, same prefix) |
| 283 | 283_rel_type_aliases_spanish_occupation.sql / 283_linguistic_cues_italian_repair_cues.sql | es/it FOSS rel-alias + repair cues |
| 290 | 290_backend_service_credential.sql | auto-minted backend service secret (#121) |
| 291 | 291_operator_admin_token.sql | persisted operator token hash (#145) |
| 292 | 292_dashboard_seat_requests.sql | refused-user seat requests (#148) |

Next free saas number: 325 (saas runs through 324 = growth_signals — the error/recall enhancement loop's capture store, 2026-11-13; 323 = sense_layer_seed — the LANGUAGE-STANDARDS-SPEC.md §2 sense layer + §8.1 recall disambiguation, 2026-11-13; 322 = seed_medical_shelf_noun_cues, BUG-FAMILY-9 residual 2 — the growth-signals lane RENUMBERED its file to 324 on landing (the coexistence note resolved); 321 = seed_hobby_activity_aspects is an in-flight sibling's file — verify it landed before reusing its number; 320 = seed_blended_family_kinship landed; do NOT reuse 290-292, and note two pre-existing 298_* files coexist by basename keying).
