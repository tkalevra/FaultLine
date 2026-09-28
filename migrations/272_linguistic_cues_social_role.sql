-- Migration 272: linguistic_cues — seed the SOCIAL-ROLE closed class ('social_role').
-- Date: 2026-08-27
--
-- WHY
-- ---
-- Measured on a FRESH seat: social_role = n=0, while role_noun = 4 and kinship_noun = 23.
-- `public.linguistic_cues` already seeds 23 classes; social_role was the one gap. Consequence: the
-- possessed-person-role rail (23832d41) is INERT on a virgin tenant, so
--
--     "My colleague works late on Thursdays."   ->   (user, owns, colleague)
--
-- files a PERSON as an OWNED OBJECT until the tenant happens to grow the class. That is THE HARD
-- LINE inverted at ingest, and it is the DEFAULT state of every new seat.
--
-- THE OWNER'S RULING THAT AUTHORISES THIS SEED
-- --------------------------------------------
-- "those aspects that are ours to control should not require validation to become useable, they
--  should be hinged on the growth. The only aspect requiring 'validation' should be the user's
--  actual memory ie. inferred class C, otherwise everything is provided and tabled straight out."
--
-- A cue class is a SHELF (engine scaffolding — ours). A fact about the user is a MEMORY. Shelves get
-- built on sight; memories get checked. Migration 123 carved social_role out on the premise that it
-- is "DOMAIN-FLAVORED" (its comment, and grow_linguistic_cue_candidates' comment at
-- re_embedder/embedder.py:10306 repeats it). That premise is OVERRULED: colleague / coworker /
-- teammate / roommate is ENGLISH STRUCTURE — the same closed class as the already-seeded
-- kinship_noun and unit_scalar — not subject matter. The line that keeps this subject-agnostic:
-- CLOSED-CLASS = English structure, SEED it; OPEN-CLASS = subject matter, GROW it.
-- Migration 123 is amended in the same change to stop deleting this category (it re-runs on EVERY
-- container start, so leaving it would delete-then-reinsert these rows on every boot and, worse,
-- would resurrect any row a user had DEACTIVATED).
--
-- SOURCED FROM WORDNET, NOT INVENTED — REPRODUCIBLE
-- -------------------------------------------------
-- Derivation script (run it and diff against the VALUES list below):
--     python3 the internal design record
-- WordNet is baked into the image (Dockerfile:64, omw-1.4) and is already the deterministic
-- hypernym source for src/api/wordnet_ladder.py. The filter, exactly:
--
--   (R) RELATIONAL RESTRICTION (Loebner's sortal/relational split — a RELATIONAL noun carries an
--       implicit argument slot the possessor fills). WordNet lexicalises this field under four
--       person-to-person anchors whose glosses name the second argument verbatim, plus `friend`:
--           peer.n.01          "a person who is of equal standing WITH ANOTHER in a group"
--           associate.n.01     "a person who joins WITH OTHERS in some activity or endeavor"
--           acquaintance.n.03  "a person WITH WHOM YOU are acquainted"
--           neighbor.n.01      "a person who lives (or is located) NEAR ANOTHER"
--           friend.n.01        (the universal social primitive already in the in-code floor)
--       MEMBERS = those synsets + their DIRECT hyponyms (depth 1). Depth 1 IS the closed-class
--       boundary: depth 2 reaches member.n.01 and from there the whole social-group tree.
--
--   (P) PERSON-DENOTING WITHOUT AMBIGUITY: every noun synset of the lemma has lexname
--       'noun.person'. This is what keeps the class TIGHT, and it is why `light`, `match`,
--       `connection`, `member`, `backup`, `relief`, `substitute`, `replacement`, `pickup`,
--       `associate`, `fellow`, `mate` are all REJECTED — their person reading exists but their
--       ordinary reading is a thing, and admitting them would turn "my connection is slow" into a
--       person tie. (21 rejections in total; the script prints every one with its reason.)
--
--   EXCLUSIONS (READ FROM THE DB, not typed): multi-word lemmas (the deriver matches a single head lemma; a HYPHENATED lemma also
--   emits its closed-up form, which is how co-worker -> coworker gets in); lemmas already seeded in
--   `kinship_noun`; and lemmas already seeded in `role_noun` (employer/boss/manager/supervisor).
--   The role_noun exclusion is DELIBERATE AND LOAD-BEARING: role_noun runs the OPPOSITE map
--   direction (possessor -> filler, `works_for`), social_role runs filler -> possessor. Seeding
--   `boss` in both would be a direction bug. boss/manager ARE covered — just not by this class.
--
--   WHY IT IS NOT TOO WIDE: the filter pulls ZERO occupations. `dermatologist` and `plumber` sit
--   under worker.n.01 / professional.n.01, which is not in the anchor closure. Those are open-class
--   subject matter a tenant GROWS.
--
-- THE MAP VALUE IS EVIDENCED, NOT GUESSED
-- ---------------------------------------
-- social_role is a KEYED class: `resolve_social_role_map` -> `_resolve_keyed_map` reads `cue` as the
-- key and `description` as the VALUE, and `_fetch_keyed_map` SKIPS any row with a NULL/empty
-- description — so a description-less row would be INVISIBLE to the resolver. The map is required.
--   * `knows` for every member: it is what the growth engine has ALWAYS written for this category
--     (re_embedder/embedder.py `_CARVED = {"social_role": "knows", ...}`, and every grown row
--     measured on this database carries description='knows'), and it is a SEEDED, SYMMETRIC
--     rel_type with head_types={Person}, tail_types={Person,Organization}.
--   * `friend` -> `friend_of`: the value already shipping in the in-code floor
--     (_BOOTSTRAP_SOCIAL_ROLE_MAP) and a seeded symmetric rel in its own right. More specific than
--     `knows`, so it wins for that one lemma. Seeding it in the DB also means the class no longer
--     depends on the code floor for its one universal member.
--   * DIRECTION CONVENTION (do not merge with role_noun): the value is the rel_type FROM the FILLER
--     TO the POSSESSOR — "my colleague is Sam" => (sam, knows, user) — the same direction as
--     kinship_noun (mother -> parent_of => (mother, parent_of, user)).
--
-- COLUMN NOTE: `example_text` carries the WordNet synset id + gloss the row was derived from. That
-- is per-row provenance; `description` is claimed by the map value, and there is no other column.
--
-- SEEDS ARE NEVER BLOWN OVER
-- --------------------------
-- Both inserts are `ON CONFLICT (cue, category) DO NOTHING`, matching 267/269/270. This file re-runs
-- on EVERY container start (docker-entrypoint.sh:155 psql -f's every migration), so a DO UPDATE here
-- would silently resurrect a user-corrected row on every deploy.
-- ⚠️ `DO NOTHING` protects a row the user EDITED or DEACTIVATED. It does NOT protect a row the user
-- DELETED — the conflict target no longer matches and the seed re-inserts. The safe correction for a
-- seeded cue is therefore DEACTIVATE (`UPDATE linguistic_cues SET is_active = false`, the path
-- documented at src/extraction/linguistics.py:6929), never DELETE. The resolvers filter on
-- `is_active`, and `_resolve_keyed_map` additionally subtracts a deactivated cue from the in-code
-- floor, so a deactivation is honoured end to end and survives every re-run of this migration.
--
-- CROSS-CLASS COLLISIONS ARE A DIRECTION BUG — PART 0 CLEANS UP AN EXISTING ONE
-- ----------------------------------------------------------------------------
-- The sibling-class exclusion is not cosmetic. `_person_role_relation`
-- (src/extraction/linguistics.py) resolves social_role BEFORE role_noun, and
-- `_possessed_person_role_rel` resolves kinship_noun BEFORE either. So a lemma in two classes does
-- not "win by being more specific" — it wins by ORDER, and silently suppresses the other class's
-- documented reading.
--   * MEASURED, and it is why Part 0 exists: migration 117 ALSO seeds social_role, and its list
--     contains `boss` and `manager`, which `role_noun` already holds. Migration 123 has been
--     DELETING migration 117's rows on every boot, so the collision was masked by the carve-out.
--     Amending 123 (this change) unmasks it: `manager` would resolve `knows` from social_role
--     instead of the role-derived `manager_of` that `_person_role_relation`'s own docstring
--     specifies. Migration 117 is amended to drop those two from its social_role list, and Part 0
--     below removes the rows already inserted — SEED-sourced rows ONLY, so a grown or
--     user-authored row is never touched.
--   * The derivation script caught the same class of bug in MY OWN list: its first cut used a
--     hand-typed kinship list, missed `partner` (kinship_noun: partner -> spouse) and would have
--     shipped a third collision. It now reads the sibling classes FROM public.linguistic_cues.
--
-- Consumed by linguistics._possessed_person_role_rel via
-- linguistic_cue_overlay.resolve_social_role_map().

-- ============================================================================
-- Part 0: remove SEED-sourced social_role rows that collide with a sibling class
-- ============================================================================
-- Idempotent, and scoped to `source LIKE 'seed_%'` so a GROWN or user-authored row is never removed
-- (a user who deliberately wants `boss` as a social tie keeps it). Runs BEFORE the insert below so a
-- re-run cannot resurrect what it just removed.
DELETE FROM public.linguistic_cues s
 WHERE s.category = 'social_role'
   AND s.source LIKE 'seed_%'
   AND EXISTS (SELECT 1 FROM public.linguistic_cues r
                WHERE r.cue = s.cue AND r.category IN ('kinship_noun', 'role_noun'));

DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.tables
                    WHERE table_schema = _schema AND table_name = 'linguistic_cues') THEN
            EXECUTE format($del$
                DELETE FROM %I.linguistic_cues s
                 WHERE s.category = 'social_role'
                   AND s.source LIKE 'seed_%%'
                   AND EXISTS (SELECT 1 FROM %I.linguistic_cues r
                                WHERE r.cue = s.cue AND r.category IN ('kinship_noun','role_noun'))
            $del$, _schema, _schema);
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- Part 1: public template
-- ============================================================================
INSERT INTO public.linguistic_cues
    (cue, category, description, example_text, source, global_confidence)
VALUES
  ('amigo', 'social_role', 'knows', 'amigo.n.01: a friend or comrade', 'seed_social_role', 0.85),
  ('bedfellow', 'social_role', 'knows', 'bedfellow.n.01: a temporary associate', 'seed_social_role', 0.85),
  ('buddy', 'social_role', 'knows', 'buddy.n.01: a close friend who accompanies his buddies in their activities', 'seed_social_role', 0.85),
  ('bunkmate', 'social_role', 'knows', 'bunkmate.n.01: someone who occupies the same sleeping quarters as yourself', 'seed_social_role', 0.85),
  ('campmate', 'social_role', 'knows', 'campmate.n.01: someone who lives in the same camp you do', 'seed_social_role', 0.85),
  ('classmate', 'social_role', 'knows', 'schoolmate.n.01: an acquaintance that you go to school with', 'seed_social_role', 0.85),
  ('coeval', 'social_role', 'knows', 'contemporary.n.01: a person of nearly the same age as another', 'seed_social_role', 0.85),
  ('collaborator', 'social_role', 'knows', 'collaborator.n.03: an associate in an activity or endeavor or sphere of common interest', 'seed_social_role', 0.85),
  ('colleague', 'social_role', 'knows', 'colleague.n.02: a person who is member of one''s class or profession', 'seed_social_role', 0.85),
  ('companion', 'social_role', 'knows', 'companion.n.01: a friend who is frequently in the company of another', 'seed_social_role', 0.85),
  ('compeer', 'social_role', 'knows', 'peer.n.01: a person who is of equal standing with another in a group', 'seed_social_role', 0.85),
  ('comrade', 'social_role', 'knows', 'companion.n.01: a friend who is frequently in the company of another', 'seed_social_role', 0.85),
  ('confidant', 'social_role', 'knows', 'confidant.n.01: someone to whom private matters are confided', 'seed_social_role', 0.85),
  ('confrere', 'social_role', 'knows', 'colleague.n.02: a person who is member of one''s class or profession', 'seed_social_role', 0.85),
  ('contemporary', 'social_role', 'knows', 'contemporary.n.01: a person of nearly the same age as another', 'seed_social_role', 0.85),
  ('cooperator', 'social_role', 'knows', 'collaborator.n.03: an associate in an activity or endeavor or sphere of common interest', 'seed_social_role', 0.85),
  ('coworker', 'social_role', 'knows', 'colleague.n.01: an associate that one works with', 'seed_social_role', 0.85),
  ('crony', 'social_role', 'knows', 'buddy.n.01: a close friend who accompanies his buddies in their activities', 'seed_social_role', 0.85),
  ('equal', 'social_role', 'knows', 'peer.n.01: a person who is of equal standing with another in a group', 'seed_social_role', 0.85),
  ('familiar', 'social_role', 'knows', 'companion.n.01: a friend who is frequently in the company of another', 'seed_social_role', 0.85),
  ('fillin', 'social_role', 'knows', 'stand-in.n.01: someone who takes the place of another (as when things get dangerous or difficult)', 'seed_social_role', 0.85),
  ('flatmate', 'social_role', 'knows', 'flatmate.n.01: an associate who shares an apartment with you', 'seed_social_role', 0.85),
  ('friend', 'social_role', 'friend_of', 'ally.n.02: an associate who provides cooperation or assistance', 'seed_social_role', 0.85),
  ('gangsta', 'social_role', 'knows', 'gangsta.n.01: (Black English) a member of a youth gang', 'seed_social_role', 0.85),
  ('girlfriend', 'social_role', 'knows', 'girlfriend.n.01: any female friend', 'seed_social_role', 0.85),
  ('homeboy', 'social_role', 'knows', 'homeboy.n.02: a male friend from your neighborhood or hometown', 'seed_social_role', 0.85),
  ('intimate', 'social_role', 'knows', 'confidant.n.01: someone to whom private matters are confided', 'seed_social_role', 0.85),
  ('messmate', 'social_role', 'knows', 'messmate.n.01: (nautical) an associate with whom you share meals in the same mess (as on a ship)', 'seed_social_role', 0.85),
  ('pal', 'social_role', 'knows', 'buddy.n.01: a close friend who accompanies his buddies in their activities', 'seed_social_role', 0.85),
  ('pardner', 'social_role', 'knows', 'collaborator.n.03: an associate in an activity or endeavor or sphere of common interest', 'seed_social_role', 0.85),
  ('participant', 'social_role', 'knows', 'participant.n.01: someone who takes part in an activity', 'seed_social_role', 0.85),
  ('peer', 'social_role', 'knows', 'peer.n.01: a person who is of equal standing with another in a group', 'seed_social_role', 0.85),
  ('reliever', 'social_role', 'knows', 'stand-in.n.01: someone who takes the place of another (as when things get dangerous or difficult)', 'seed_social_role', 0.85),
  ('roomie', 'social_role', 'knows', 'roommate.n.01: an associate who shares a room with you', 'seed_social_role', 0.85),
  ('roommate', 'social_role', 'knows', 'roommate.n.01: an associate who shares a room with you', 'seed_social_role', 0.85),
  ('roomy', 'social_role', 'knows', 'roommate.n.01: an associate who shares a room with you', 'seed_social_role', 0.85),
  ('schoolfellow', 'social_role', 'knows', 'schoolmate.n.01: an acquaintance that you go to school with', 'seed_social_role', 0.85),
  ('schoolfriend', 'social_role', 'knows', 'schoolfriend.n.01: a friend who attends the same school', 'seed_social_role', 0.85),
  ('schoolmate', 'social_role', 'knows', 'schoolmate.n.01: an acquaintance that you go to school with', 'seed_social_role', 0.85),
  ('shipmate', 'social_role', 'knows', 'shipmate.n.01: an associate on the same ship with you', 'seed_social_role', 0.85),
  ('sidekick', 'social_role', 'knows', 'buddy.n.01: a close friend who accompanies his buddies in their activities', 'seed_social_role', 0.85),
  ('standin', 'social_role', 'knows', 'stand-in.n.01: someone who takes the place of another (as when things get dangerous or difficult)', 'seed_social_role', 0.85),
  ('teammate', 'social_role', 'knows', 'teammate.n.01: a fellow member of a team', 'seed_social_role', 0.85),
  ('townsman', 'social_role', 'knows', 'townsman.n.01: a person from the same town as yourself', 'seed_social_role', 0.85),
  ('workfellow', 'social_role', 'knows', 'colleague.n.01: an associate that one works with', 'seed_social_role', 0.85)
ON CONFLICT (cue, category) DO NOTHING;

-- ============================================================================
-- Part 2: Per-user schemas (loop over faultline_* schemas)
-- ============================================================================
DO $$
DECLARE
    _schema TEXT;
BEGIN
    FOR _schema IN
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name LIKE 'faultline\_%'
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = _schema AND table_name = 'linguistic_cues'
        ) THEN
            EXECUTE format($seed$
                INSERT INTO %I.linguistic_cues
                    (cue, category, frequency, confirmed_count, rejected_count,
                     correction_count, global_confidence, description, example_text,
                     source, is_active, archived_at, last_matched_at)
                SELECT cue, category, frequency, confirmed_count, rejected_count,
                       correction_count, global_confidence, description, example_text,
                       source, is_active, archived_at, last_matched_at
                FROM public.linguistic_cues
                WHERE category = 'social_role'
                ON CONFLICT (cue, category) DO NOTHING
            $seed$, _schema);

            RAISE NOTICE 'Migration 272: social_role seeded in %', _schema;
        END IF;
    END LOOP;
END $$;
