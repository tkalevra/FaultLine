-- 314_negative_space_sensitivity_shelf.sql — SEC-BUG-3 (the serve-lane critic F1
-- MAJOR): THE SENSITIVITY-CLASS SHELF the negative-space serve reads.
--
-- WHAT
-- ----
-- One ADDITIVE metadata column on rel_types in EVERY schema that holds the table:
--
--     is_sensitive BOOLEAN NOT NULL DEFAULT false
--
-- plus THE DECLARED CLASS ADMISSION: every existing rel_types row whose name (or
-- ingest category) falls in the declared sensitivity classes — medical_condition,
-- medication, phone, email, contact, financial, credential — is stamped
-- is_sensitive = true, by three deterministic admissions:
--   (a) the EXACT declared rel_type names — the surfaces the store actually mints
--       (has_medical_condition / has_medication / has_phone / has_email / ... the
--       measured leak class: seat 6e25c1a3's "how old is my sister?" served diabetes,
--       metformin and 555-0142 under the disclosure);
--   (b) the ingest-side's own PRIVATE categories (contact / identity / network) —
--       src/api/main.py::_scalar_sensitivity_for already derives
--       entity_attributes.sensitivity='private' from exactly these, so the shelf can
--       never disagree with the ingest stamp;
--   (c) the DECLARED TOKEN PATTERN (word-boundary prefix regex) so a novel spelling
--       the exact list misses (has_blood_pressure, work_email, credit_score) is still
--       marked.
-- NO backfill beyond the stamp, NO row deletion, NO behavior-bearing metadata touched
-- (label / correction_behavior / tail_types / storage_target untouched — the shelf
-- declares SENSITIVITY, never a serving slot; nothing here routes storage or changes
-- the WGM gate).
--
-- WHY
-- ---
-- The negative-space serve (RECALL_NEGATIVE_SPACE, src/mcp/server.py) serves the
-- topic's closest registered facts when the walk + the episodic band + the archive
-- band ALL abstain. Row selection gated only on live/affirmed/¬context — NO
-- sensitivity gate — so an aspect-miss question converted into the topic's three most
-- recent rows, whatever they held. The owner's bar explicitly includes medical
-- conditions ("reliably, without eating shit"); the mandatory disclosure clause made
-- it HONEST leaking — still leaking. The shelf is the broad-serve WITHOLD list: rows
-- whose rel is marked sensitive never ride the negative-space broad-serve. They still
-- serve on DIRECT aspect asks through the normal walk gates — the shelf gates the
-- negative-space serve only, never the walk.
--
-- The RUNTIME AUTHORITY for names that never registered as vocabulary (a novel rel
-- minted after this migration, or a bare fixture schema without rel_types) is the
-- deterministic token matcher in src/mcp/server.py
-- (_negative_space_name_is_sensitive) admitting the SAME classes token-wise; the DB
-- stamp and the code matcher are the two halves of one declared contract (the code
-- prefix rule is length-gated at >=5 so short class words like 'pin'/'ssn' stay
-- exact-only there; this file's regex stamps the DB side slightly wider — the same
-- conservative direction: over-withholding on the broad-serve only, direct asks
-- unaffected).
--
-- WHY A MIGRATION NOW
-- -------------------
-- Template + migration, never one without the other (the 197/273/298/299/300/301
-- rule): src/provisioning/templates/user_schema.sql gains the same column in the same
-- commit, appended LAST (the ordinal discipline — schema_manager bootstraps rel_types
-- with INSERT INTO rel_types SELECT * FROM public.rel_types, so a fresh-mint schema
-- and a migrated schema must agree on column ordinal). ADDITIVE + IDEMPOTENT only:
-- ADD COLUMN IF NOT EXISTS and a pure is_sensitive stamp. Fresh tenants minted AFTER
-- this migration copy the stamped public rows through the bootstrap INSERT.
--
-- FAN-OUT: every schema holding a rel_types table (public + faultline_% + flagger_% +
-- pin/seat schemas), the 308/309/313 pattern; the guarded per-schema loop skips a
-- partially-provisioned schema instead of aborting the whole DO block (the b99ce200
-- discipline: one bad schema must never skip the rest).
DO $mig$
DECLARE
    _sch text;
BEGIN
    FOR _sch IN
        SELECT n.nspname
          FROM pg_namespace n
         WHERE EXISTS (
               SELECT 1
                 FROM pg_class c
                WHERE c.relnamespace = n.oid
                  AND c.relname = 'rel_types'
                  AND c.relkind = 'r')
         ORDER BY n.nspname
    LOOP
        BEGIN
            EXECUTE format('ALTER TABLE %I.rel_types ADD COLUMN IF NOT EXISTS'
                           ' is_sensitive BOOLEAN NOT NULL DEFAULT false', _sch);
            EXECUTE format($ddl$
                UPDATE %I.rel_types
                   SET is_sensitive = true
                 WHERE is_sensitive IS DISTINCT FROM true
                   AND (
                        -- (a) the EXACT declared class surfaces
                        lower(rel_type) = ANY(ARRAY[
                            'medical_condition','has_medical_condition',
                            'medical_conditions','has_medical_conditions',
                            'health_condition','has_health_condition',
                            'has_condition','diagnosis','has_diagnosis',
                            'diagnoses','has_diagnoses','medical_issue',
                            'illness','has_illness','disease','has_disease',
                            'allergy','has_allergy','allergies','has_allergies',
                            'medication','has_medication','medications',
                            'has_medications','prescription','has_prescription',
                            'medicine','has_medicine','takes_medication',
                            'takes_medicine','drug','has_drug',
                            'phone','has_phone','phone_number','has_phone_number',
                            'telephone','has_telephone','mobile','has_mobile',
                            'cellphone','has_cellphone','contact_number',
                            'email','has_email','email_address',
                            'has_email_address','contact_email',
                            'contact','has_contact','contact_info',
                            'has_contact_info','contact_information',
                            'financial','has_financial','finance','bank','has_bank',
                            'bank_account','has_bank_account','banking',
                            'account_number','has_account_number','iban','has_iban',
                            'credit_card','has_credit_card','card_number',
                            'has_card_number','credit_score','has_credit_score',
                            'salary','has_salary','income','has_income','wage',
                            'has_wage','net_worth','has_net_worth','debt','has_debt',
                            'loan','has_loan','mortgage','has_mortgage',
                            'routing_number','has_routing_number','tax_id',
                            'has_tax_id','insurance','has_insurance',
                            'financial_account','has_financial_account',
                            'password','has_password','passphrase','has_passphrase',
                            'credential','has_credential','credentials',
                            'api_key','has_api_key','access_token',
                            'has_access_token','secret','has_secret','pin','has_pin',
                            'ssn','has_ssn','social_security_number',
                            'has_social_security_number','security_question',
                            'has_security_question','recovery_code',
                            'has_recovery_code','otp','has_otp'
                        ])
                        -- (b) the ingest-side's own PRIVATE categories
                        OR COALESCE(lower(category), '') =
                            ANY(ARRAY['contact','identity','network'])
                        -- (c) the declared token pattern (prefix at word boundary)
                        OR lower(rel_type) ~
                           '(^|_)(medical|medication|medicine|diagnos|symptom|condition|illness|disease|allerg|prescription|drug|pharmacy|dosage|insulin|glucose|blood|cancer|therapy|vaccine|health|diabetes|asthma|epilepsy|arthritis|hypertension|depression|anxiety|syndrome|disorder|infection|phone|telephon|mobile|cellphone|email|contact|financial|financ|bank|salary|income|wage|credit|debit|iban|loan|debt|mortgage|wallet|insurance|password|passphrase|credential|secret|api|token|otp|ssn|pin)'
                      )$ddl$, _sch);
        EXCEPTION
            WHEN undefined_table THEN NULL;  -- rel_types dropped between probe and ALTER
            WHEN OTHERS THEN NULL;           -- a partially-provisioned schema never
        END;                                 -- aborts the whole fan-out (b99ce200)
    END LOOP;
END
$mig$;
