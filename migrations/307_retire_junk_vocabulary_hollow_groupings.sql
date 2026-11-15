-- 307: retire junk growth vocabulary + hollow groupings (BUG-05 + BUG-03-remainder,
--       DEV/BUGS/prod-document-drain-20261103/05 + /03).
--
-- The growth seams minted (a) negated/modal phrases as rel_types (polarity columns own
-- that reading - the not_*/cannot*/never_* family proves mints that bypassed polarity
-- routing), (b) non-normal-form and sentence-length "relations" ("cannot handle"), and
-- (c) PEOPLE/PLACE names lifted into the predicate slot by the citation frame (beltagy,
-- cattan, usa, ...). The mint-time guards (src/extraction/rel_vocab.py via
-- REL_TYPE_VOCAB_GUARD) stop NEW mints; this migration retires the measured stock.
--
-- SAFETY: rel_types rows are deleted - facts/staged_facts carry the rel STRING (no FK),
-- so user truth (facts, staged, episodic_log) is untouched; a deleted rel simply becomes
-- unknown again, and the mint veto now refuses to resurrect junk. Only the measured
-- lists from the tickets + the same structural rules the code guard enforces are used -
-- no new lexical zoo. Re-filing staged author-name uses as content edges (created_by)
-- needs the source sentence and is deliberately NOT done here (documented residual).
--
-- AUTHORITY (user > seed > growth): ONLY ENGINE-GROWN rows are eligible for deletion
-- (engine_generated IS TRUE - the growth mints stamp it, seed wikidata/builtin and
-- user-authored rels keep it false). A user's own rel_type with a negator in the name is
-- USER VOCABULARY, not growth junk; the structural rules below must never touch it.
--
-- Idempotent: every statement is a no-op once the rows are gone.

-- (1) vocabulary normal form + token cap - the SAME strict rule the code guard enforces
--     (src/extraction/rel_vocab.py: ^[a-z]+(?:_[a-z]+)*$ with <= 6 tokens).
--     CAP HISTORY (both measurements pinned, do not re-litigate): the SEED vocabulary
--     maxes at 3 tokens (migration 005, 26 rows, longest also_known_as) - the original
--     3-cap measured the seed floor; but the GROWN tail on prod reaches 5 tokens
--     (stores_long_term_memory_in 5 tokens/26 chars; max_percentage_during_storage
--     4 tokens/29 chars), which a 3-cap false-vetoes - even a 4-cap clips the
--     5-token row. The shipped code cap is 6
--     (2x the seed floor, admits the whole measured grown tail, refuses the measured
--     7-token clause fragments). This migration must match the code guard it mirrors.
DELETE FROM rel_types
 WHERE engine_generated IS TRUE
   AND (rel_type !~ '^[a-z]+(_[a-z]+)*$'
        OR array_length(string_to_array(rel_type, '_'), 1) > 6);

-- (2) negation family - closed UD-negator tokens as whole tokens (bare "no" kept: a
--     genuine noun like no_show is not a negated predicate; "without" kept too - it is a
--     preposition, and works_without_pay is legitimate vocabulary, not a negated
--     reading). Matches the closed-class fallback set _CLOSED_NEGATOR_LEMMAS in
--     src/extraction/rel_vocab.py exactly (17 lemmas; the parse-based UD-neg gate is
--     the authority at mint time - this stock cleanup uses the closed-class shape).
DELETE FROM rel_types
 WHERE engine_generated IS TRUE
   AND rel_type ~ '(^|_)(not|never|cannot|cant|dont|doesnt|didnt|isnt|arent|wasnt|werent|wont|wouldnt|couldnt|shouldnt|nor|neither)(_|$)';

-- (3) measured name/place rel_types (BUG-03 evidence list, seat 10d7d879 inspection).
DELETE FROM rel_types
 WHERE engine_generated IS TRUE
   AND rel_type IN ('beltagy','cattan','see','zhuoyi','zekai','yuanzhi','xiangrui','vipula','vinija','usa','rizwan','shreshtha','kul','askell','cohan');

-- (4) drain the quarantine queue of the same classes (the valve must not keep carrying
--     candidates the mint can never legitimately place). The queue is engine-produced by
--     construction (mint candidates never enter from user writes), so no authority
--     filter is needed here - only the same strict rules as (1)-(3).
DELETE FROM ontology_evaluations
 WHERE candidate_rel_type !~ '^[a-z]+(_[a-z]+)*$'
    OR array_length(string_to_array(candidate_rel_type, '_'), 1) > 6
    OR candidate_rel_type ~ '(^|_)(not|never|cannot|cant|dont|doesnt|didnt|isnt|arent|wasnt|werent|wont|wouldnt|couldnt|shouldnt|nor|neither)(_|$)'
    OR candidate_rel_type IN ('beltagy','cattan','see','zhuoyi','zekai','yuanzhi','xiangrui','vipula','vinija','usa','rizwan','shreshtha','kul','askell','cohan');

-- (5) hollow ENGINE-GROWN groupings: defining rel list empty = scopes nothing at
--     determine_path while reading identical to a healthy row (61/574 at filing,
--     dominated by paper front-matter places). SEEDED rows are never touched (the
--     seeded "animal" grouping is hollow in the template BY DESIGN and protected here
--     by the source filter, exactly like the mint ON CONFLICT guard).
DELETE FROM entity_taxonomies
 WHERE source = 'engine_grown_ingest'
   AND (rel_types_defining_group IS NULL OR cardinality(rel_types_defining_group) = 0);