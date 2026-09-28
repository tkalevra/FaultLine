"""ABBREVIATION-DEFINITION EXTRACTION — a statable LICENCE for a co-reference claim.

WHAT THIS IS FOR
----------------
``entity_aliases`` is modelled on SKOS lexical labels, so adding a SECOND, different surface
to an entity ASSERTS that both surfaces denote one referent (see
``src/entity_registry/weld_guard.py`` for the full argument and the production measurement).
That makes it a truth claim, and the engine records, separately from how much a source is
TRUSTED, whether its claim was LICENSED by anything at all — ``registry._WARRANTED_SOURCES``
/ ``registry.preference_is_warranted()``.

Before this module, the generic extraction ``also_known_as`` / ``pref_name`` lane emitted
WARRANTED co-reference (a genuine abbreviation definition the user wrote) and UNWARRANTED
co-reference (an extraction guess) INDISTINGUISHABLY — identical ``preference_source``, no
recorded licence. No rule downstream can separate what the producer conflated. Measured on
production, that single conflation was the ONLY thing keeping the weld guard's third arm
disarmed: of 48 refusals, 47 were the exact hard-line corruption it targets and ONE —
``{sustainable aviation fuel} ≡ {saf}`` — was a real acronym definition.

This module lets the PRODUCER name its licence: when the user's own sentence defines the
short form against the long form, the co-reference is warranted, and the ingest lane records
it. Nothing here decides anything about trust, display preference, or storage.

THE ALGORITHM AND ITS SOURCE
----------------------------
Schwartz, A.S. & Hearst, M.A., "A simple algorithm for identifying abbreviation definitions
in biomedical text", Pacific Symposium on Biocomputing 8:451-462 (2003), PMID 12603049.

The abstract states the algorithm "achieves 96% precision and 82% recall on a standard test
collection" and "95% precision and 82% recall" on a larger set, and that "unlike other
approaches, it does not require any training data".

⚠️ CITATION PRECISION, because this matters more than the convenience of a label. THE ONLY CLAIM
THAT SURVIVES CHECKING IS THE NARROW ONE: the paper never applies the phrase "rule-based" to the
simple algorithm ported here. An earlier version of this docstring said it "reserves that word
for the approaches it compares against (Park & Byrd; Yu et al.)" — that is true of the BODY (§2,
p.453: "Park and Byrd present a rule-based algorithm…", "Yu et al. present another rule-based
algorithm…") but it is INCOMPLETE in a way that matters, because the paper's own reference 16 is

    "A. Schwartz, and M. Hearst, 'A Rule Based Algorithm for Identifying Abbreviation
     Definitions in Biomedical Text Using Decision Lists' University of California, Berkeley,
     Technical Report, to appear."

i.e. the AUTHORS applying the phrase to their OWN earlier work. Reference 16 is cited from §5
("An Alternative Algorithm"), which describes a DIFFERENT, ABANDONED method — "a much more
complex algorithm than that presented here… a variation on the decision lists algorithm… The
algorithm makes use of training data". So "rule-based" names the decision-list algorithm they
DISCARDED, not the subsequence matcher ported below; the Conclusions position the ported one
against that whole category ("less specific – and therefore less potentially brittle – than
other approaches that use carefully crafted rules"). "Training-free" is the paper's claim and is
quoted above; "deterministic" is OUR characterisation of a matcher with no learned parameters,
and is recorded here as ours. The abstract was read directly (PubMed); §§1-5, the Figure 1 Java
listing and the bibliography were read directly from the PSB proceedings PDF
(psb.stanford.edu/psb-online/proceedings/psb03/schwartz.pdf) via ``pdftotext -layout``. ⚠️ That
PDF carries NO printed folios; printed page numbers quoted here are inferred as
``printed = PDF page + 449``. SECTION numbers are printed and are safe — prefer them.

The paper's §3.1 ("Identifying Short Form and Long Form Candidates") specifies:

    "Abbreviation candidates are determined by adjacency to parentheses. The two cases are:
     (i) long form '(' short form ')'  (ii) short form '(' long form ')' … Whenever the
     expression inside the parentheses includes more than two words, pattern (ii) is assumed,
     and a short form is searched for just before the left parenthesis (word boundaries are
     indicated by spaces). Short forms are considered valid candidates only if they consist of
     at most two words, their length is between two to ten characters, at least one of these
     characters is a letter, and the first character is alphanumeric."

    "The long form candidate must appear in the same sentence as the short form, and … it
     should have no more than min(|A| + 5, |A| * 2) words, where |A| is the number of
     characters in the short form. … we consider only long forms that are adjacent to the
     short form. For a given short form, a long form candidate is composed of contiguous words
     from the original text that include the word just before the short form."

§3.2 gives the matcher (Figure 1, ``findBestLongForm``), ported verbatim in
``find_best_long_form`` below, plus one rule the paper states in prose and explicitly notes is
"omitted from Figure 1":

    "To increase precision, the algorithm discards long forms that are shorter than the short
     form, or that include the short form as one of the words in the long form."

THE CONSTRAINT THIS MODULE ADDS ON TOP, AND WHY IT IS NOT OPTIONAL
------------------------------------------------------------------
⚠️ READ THIS BEFORE RELAXING ANYTHING BELOW. Ported unmodified, Schwartz & Hearst LICENSES THE
EXACT CORRUPTION THE CONSUMING GUARD EXISTS TO REFUSE, and it does so by design, not by a port
bug. Re-read the §3.2 rule quoted above: only the FIRST character of the short form is
constrained to fall "at the beginning of a word in the long form". Every other character may
match ANYWHERE inside the long form. So:

    "I have a golden retriever (Goldie)."   →  g-o-l-d-i-e IS an ordered subsequence of
                                               "golden retriever", first char at a word start.

That is a NAME parenthesised after its TYPE PHRASE, and it is ORTHOGRAPHICALLY IDENTICAL to an
abbreviation definition — the same brackets, the same adjacency, the same subsequence property.
Measured on the shipped matcher before this constraint existed, 11 of 20 constructed
name-after-type cases were licensed ({goldie}≡{golden retriever}, {bo}≡{border collie},
{sam}≡{siamese cat}, {stan}≡{station wagon}, {nala}≡{national park}, …). Those are verbatim the
``{cat} ≡ {luna}`` shape the weld guard's third arm exists to decline, and a warrant would have
ADMITTED them PAST that arm. Zero occurrences in the current production corpus is CORPUS LUCK,
not a property of the rule.

THE DISCRIMINATOR — **LEADING-LETTER MATCHING** (ours in this codebase, but NOT invented here;
layered ON TOP of the faithful port, never inside it). A short form is admitted only when EVERY
alphanumeric character of it aligns, in order, to a WORD-INITIAL position of the long form —
``is_leading_letter_match`` below. "Word-initial" is the paper's own boundary test, reused rather
than reinvented: §3.2 locates "the beginning of a word" by decrementing "until it reaches a
non-alphanumerical character or reaches the beginning of the long form", which also makes each
part of a hyphenated word initial ("this initial position can be the first letter of a word that
is connected to other words by hyphens and other non-alphanumeric characters", §3.2).

    {saf} ≡ {sustainable aviation fuel}   s|ustainable a|viation f|uel     → ADMITTED
    {goldie} ≡ {golden retriever}         g|olden r|etriever  (o,l,d,i,e
                                          fall INSIDE words)               → REJECTED

The separation is structural, not a heuristic tuned to these examples: an abbreviation DISTRIBUTES
its characters across the words of its expansion, whereas a name shares a PREFIX RUN with one word
of the type phrase and then diverges. Nothing about pets, people, places or any other subject is
consulted; the test is pure orthography over the two surfaces.

WHERE THIS RULE COMES FROM (named by its established term, and read directly)
* Larkey, L.S., Ogilvie, P., Price, M.A. & Tamilio, B., "Acrophile: an automated acronym
  extractor and server", Proc. ACM 5th Int. Conf. on Digital Libraries (DL '00), Dallas TX,
  pp. 205-214, 2000. They enumerate matching schemes by name; scheme 2 is the relevant one.
  ⚠️ CORRECTED — AN EARLIER REVISION QUOTED THIS SCHEME BY OMISSION WHILE CLAIMING VERBATIM
  FIDELITY ("scheme 2 IS this constraint, character for character"). Scheme 2 IN FULL is TWO
  sentences, and only the first was quoted or implemented:

      "**Lowercase strict**: each letter in the acronym must be represented, in order, by the
       first letter of a word in the expansion. The expansion must begin with the first letter
       of the acronym and must not contain uppercase letters."

  WHAT IS IMPLEMENTED: sentence 1, exactly — that is ``is_leading_letter_match``.
  WHAT IS NOT, and why each is deliberate rather than overlooked:
    (a) "must begin with the first letter of the acronym" — NOT enforced by the function. It is
        enforced STRUCTURALLY one layer out instead: Schwartz & Hearst's own ``find_best_long_form``
        returns ``long_form[l_index:]`` where ``l_index`` is the start of the word holding the
        FIRST matched character, so every pair ``licensed_pairs`` emits already begins with the
        acronym's first letter. Verified by execution — "We use my big golden retriever (GR)
        daily." licenses ``{golden retriever}``, not the wider span. So the pipeline satisfies
        the clause; the FUNCTION does not, and it is PUBLIC, so a direct caller (this module's
        own test suite is one) can reach the looser behaviour: measured,
        ``is_leading_letter_match('saf', 'the sustainable aviation fuel')`` and
        ``('gr', 'my big golden retriever')`` both return True, and Larkey's sentence 2 rejects
        both. Standalone, the function is LOOSER than the scheme it was said to be verbatim.
    (b) "must not contain uppercase letters" — deliberately NOT ported. It is a CASE HEURISTIC
        for distinguishing an acronym from its expansion in running text, and this port
        lowercases both surfaces before comparing (``_norm``), so the clause has nothing to act
        on. Larkey's scheme 1 ("Canonical") is the uppercase-sensitive sibling; porting the
        case clause would import a scheme boundary this module does not use.
  Their scheme 3 ("Uppercase loose") is the failure mode being escaped: "This scheme is extremely
  loose, and can result in expansions where some letters in the acronym are not matched at all."
  ⚠️ Read from the CIIR technical-report version (IR-186, cs.cmu.edu/~pto/papers/ciir/ir-186.pdf,
  `pdftotext -layout`); its internal pagination does NOT match the DL '00 page range.
* Park, Y. & Byrd, R.J., "Hybrid text mining for finding abbreviations and their definitions",
  Proc. EMNLP 2001, pp. 126-133 (ACL Anthology W01-0516) — §3.1 factors abbreviation formation
  into positional codes and names this one 'F': "'F' means that the first character of a word
  occurs in the abbreviation. Similarly, 'I' refers to an interior character and 'L' indicates
  the last character of a word." This constraint admits 'F' only.
* Chang, J.T., Schütze, H. & Altman, R.B., "Creating an online dictionary of abbreviations from
  MEDLINE", J Am Med Inform Assoc 9(6):612-620, 2002 (PMC349378) — describes it as the base case:
  "The simplest type of algorithm matches an abbreviation's letters to the initial letters of the
  words around it."

⚠️ TERMINOLOGY — "INITIALISM" IS THE NEAR-MISS TERM AND IS DELIBERATELY NOT USED. An earlier draft
of this module called the function ``is_initialism_of``. Checked against a dictionary rather than
assumed: The American Heritage Dictionary of the English Language, 5th ed. (2022) distinguishes
*acronym* from *initialism* by PRONUNCIATION, not by formation — "The distinguishing feature of an
acronym is that it is pronounced as if it were a single word… Acronyms are often distinguished
from initialisms like FBI and NIH, whose individual letters are pronounced as separate syllables"
— and its *initialism* entry also admits SYLLABLE-initial matter ("TNT for trinitrotoluene"),
which this rule REJECTS. So neither word names the constraint; "leading-letter matching" describes
it and "lowercase strict" (Larkey) names the scheme. (Quirk et al. CGEL Appendix I and Huddleston
& Pullum CGEL §19.2.2 "Initialism" were sought as grammar anchors and NOT obtained — no readable
copy. They are not cited, because they were not read.)

WHAT IT COSTS, STATED (do not discover this later)
--------------------------------------------------
⚠️ THE PAPER'S AUTHORS WOULD ADVISE AGAINST THIS CHANGE, AND THAT IS RECORDED HERE RATHER THAN
OMITTED. §3.2: "By contrast, adding additional constraints, as is done by most other algorithms,
does not seem to help much in terms of precision, but can severely reduce the recall." Their
motivating counterexample is §1's "Gcn5-related N-acetyltransferase (GNAT)", which this constraint
rejects. Two things reconcile that with shipping it anyway, and neither is "the paper is wrong":
  (a) DIFFERENT CORPUS. They measure biomedical abstracts, where internal-letter matches are
      "a common occurrence"; this engine reads conversational turns. Measured here on 5 443 real
      production turns, the constraint removed 5 of 12 pairs and ALL 3 spurious ones — 9/12
      genuine became 7/7. That is a precision gain on THIS corpus, and it is not transferable.
  (b) DIFFERENT CONSUMER. Their output is a dictionary, where a missed definition is the only
      cost. Here the output is a LICENCE TO WELD TWO REFERENTS TOGETHER, consumed by a guard whose
      entire purpose is refusing exactly the construction the relaxation admits. The error costs
      are not symmetric the way they are for a dictionary.
Their measured own-corpus numbers bound the recall being given up honestly: of 169 missed pairs,
"12 (7%) have first-character matches to internal letters" — but note that counts a DIFFERENT
relaxation than the one dropped here, so it is a floor, not the figure. The right order of
magnitude comes from Chang et al., who report over MEDLINE that "328,874 (42.1%) are acronyms
(i.e., they are composed of the first letters of words)" and conclude "since less than half of all
abbreviations are formed from the initial letters of words, automated methods must handle more
sophisticated and nonstandard constructs". On a biomedical corpus this constraint would therefore
forgo roughly half the true definitions. It is a deliberate precision-for-recall trade of the kind
Larkey et al. measured directly (their strict schemes score .94-.96 precision at .56-.57 recall
against .87/.84 for their loosest — ⚠️ their schemes differ on several axes at once, so that is
evidence for the DIRECTION of the trade, not an ablation of this constraint alone).

Concretely, it rejects genuine NON-INITIAL abbreviations — CONTRACTIONS/TRUNCATIONS, where the
short form is a prefix or squeeze of ONE word: {config}≡{configuration}, {theo}≡{theodore}, and the
paper's own mixed-case shape {hb}≡{hemoglobin}. Those keep today's unwarranted provenance, which
is exactly today's behaviour. The trade is deliberate and it is forced: a truncation abbreviation
({config} from "configuration") and a truncation NICKNAME ({theo} from "Theodore") are the SAME
orthographic object, so NO rule over these two surfaces alone can admit one and refuse the other.
Refusing both is the direction that cannot corrupt.

This is a STRICT SUBSET of what the paper licenses, so it can only ever withhold a warrant, never
grant one the paper would not. That is the safe direction (see ERROR DIRECTION).

⚠️⚠️ WHAT THIS CONSTRAINT DOES **NOT** CLOSE — READ THIS BEFORE QUOTING ANY COVERAGE NUMBER.
An earlier revision of this docstring described the leftover as a rare "RESIDUAL" that was "far
rarer than the prefix-run family this constraint kills". THAT WAS FALSE, and it was false in the
direction that flatters the constraint. Re-measured by execution on the builder's OWN 20
constructed name-after-type cases, with each nickname swapped for an initialism-shaped one:

    FAMILY A — PREFIX-RUN NICKNAME ({goldie} ≡ {golden retriever}). **CLOSED.** The nickname
      shares a prefix run with ONE word of the type phrase and then diverges, so its interior
      characters are not word-initial. 11 of the builder's 20 were licensed before this
      constraint; 0 after. This is the family the constraint was written for and it works.

    FAMILY B — INITIALISM-SHAPED NICKNAME ({gr} ≡ {golden retriever}). **FULLY OPEN.** The
      constraint does nothing at all here, by construction: a nickname whose letters happen to
      align to the type phrase's word initials satisfies the rule exactly as a genuine acronym
      does. Measured on the same 20 long forms: **18 of 20 admitted** at the function level
      (``is_leading_letter_match``), **16 of 20 admitted END TO END** through
      ``text_licenses_coreference``. End to end, "I have a golden retriever (GR)." IS LICENSED.

    ⇒ ON THE BUILDER'S OWN CASE LIST THE OPEN FAMILY (16) IS **LARGER** THAN THE CLOSED ONE (11).
      "Far rarer" was the opposite of the measurement. The correct summary is: this constraint
      closes ONE of the two ways a name gets parenthesised after its type phrase, and the one it
      leaves open is the bigger half of that list.

THE TWO REJECTIONS ARE ARITHMETIC, NOT SEMANTICS — and that makes the finding worse, not better.
The only Family-B cases that fail ({lt} ≡ {laptop}, {ct} ≡ {cat}; end to end also {de} ≡
{theodore}, {co} ≡ {company}) fail because a ONE-WORD long form has exactly ONE word-initial, so
no two-character short form can align. The rule did not recognise a name; it ran out of words.
Proof that the protection is word-count arithmetic and nothing else: HYPHENATE the same one-word
type and the bypass returns, because §3.2's own boundary test — the one reused in
``_word_initial_chars`` — makes each part of a hyphenated word word-initial:

    is_leading_letter_match('gr',  'golden-retriever') → True
    is_leading_letter_match('eb',  'e-bike')           → True
    is_leading_letter_match('ts',  't-shirt')          → True
    is_leading_letter_match('mil', 'mother-in-law')    → True

So the discriminator's real separation is NOT "abbreviation vs name". It is "does the short form
distribute across at least ``len(short_form)`` word-initials of the long form" — and a NAME that
happens to do that is admitted with no hesitation whatsoever.

WHY IT IS NOT CLOSED HERE. The two readings are orthographically IDENTICAL under any rule over
these two surfaces alone: {gr} ≡ {golden retriever} is exactly the shape of {saf} ≡ {sustainable
aviation fuel}, which is the single real production pair this whole mechanism exists to preserve.
Separating them requires a signal from OUTSIDE the pair — the graph, the entity's type, or the
rest of the turn. Any attempt must clear the standing bar: still admit {saf}, reject Family B,
and be measured on real data in BOTH error directions. No such rule is shipped, and one was not
bolted on to make this docstring's old claim true.

WHAT THIS DOES AND DOES NOT MEAN FOR ARMING ARM 3 (stated because it is easy to get backwards):
the bypass costs COVERAGE, not CORRECTNESS RELATIVE TO TODAY. Today every weld at this seam
passes. Armed, the real corruptions are refused and Family B merely CONTINUES to pass. So this
finding does not by itself disqualify arming — it disqualifies the CLAIM that the class is
closed, and it is a third reason to run OBSERVE first (``weld_guard.arm3_warrant_admitted`` is
logged at refusal level precisely so an observe run measures this leak direction too).

Zero Family-B instances occur in the current 5 443-turn production corpus. That is CORPUS LUCK
and a statement about today's users, NOT a property of the rule — the same sentence the module
already applies to the pre-constraint Family A, and it applies here with equal force.

WHY THIS SATISFIES THE ENGINE'S CONSTRAINTS
-------------------------------------------
* DETERMINISTIC, NOT FUZZY. No embeddings, no similarity score, no threshold, no substring or
  ILIKE test. The decision is an exact ordered character-subsequence match under a first-
  character-at-a-word-boundary constraint. A pair either satisfies it or it does not.
* SUBJECT-AGNOSTIC. There is no word list, no domain vocabulary, no entity/type/rel name, and
  no root anywhere in this file. The only literals are ORTHOGRAPHIC primitives — the
  parenthesis characters the paper's method is defined on, and sentence terminators — which
  are properties of the writing system, not of any subject. The engine's growth rail is
  untouched: this grants a licence, it never mints, names or classifies anything.
* NO NETWORK, NO MODEL, NO TRAINING DATA, NO LLM. Pure string arithmetic.

ERROR DIRECTION (deliberate; do not invert)
-------------------------------------------
A MISSED licence leaves the alias exactly as it arrives today — that half is unchanged and is
the reason every ambiguity in this file is resolved toward returning nothing.

⚠️ CORRECTED — THE OTHER HALF USED TO SAY "which is also exactly today's behaviour", AND THAT
WAS FALSE. It is the sentence a reader quotes, and it was stale with respect to the consuming
site's own finding (``src/api/main.py``, the ARM-3 gate comment). A wrongly granted licence does
NOT reduce to today's behaviour, for a reason that only shows up once you look at what the
warrant is STORED AS:

  * The warrant is recorded by REWRITING ``preference_source`` to ``lexical``, and the reachable
    band at that call site is exactly {``rel_default``, ``inferred``}. Since
    rank(``rel_default``) = 4 and rank(``lexical``) = 3, granting a warrant to a ``rel_default``
    alias is a TRUST DEMOTION on a second axis, not just a licence on the warrant axis.
  * Driving the REAL ``EntityRegistry.register_alias``, that demotion DIVERGED on 2 of 2 measured
    scenarios: (a) with an incumbent preferred alias at rank 4 the new alias stores
    ``is_preferred=False`` — a DIFFERENT RENDERED DISPLAY NAME; (b) with the same surface
    preferred on ANOTHER entity at rank 3, the cross-entity provenance override stops firing and
    the write instead STAGES a pending ``entity_name_conflicts`` row for LLM arbitration.

So the honest statement is: while ARM 3 is ``off`` the rewrite DOES NOT RUN AT ALL, so a false
licence costs nothing because nothing is granted. Once ARM 3 is armed — the ONLY state in which
this module's output is consumed — a false licence is today's weld **PLUS** that trust demotion
and its two observable effects. It is still bounded (it admits ONE unwarranted co-reference and
can never refuse a surface the user stated), but "exactly today's behaviour" is the wrong
summary and it understated the dangerous direction. See ``main.py``'s ARM-3 gate comment and
the internal design record §9.5.

KNOWN LIMITS, STATED
--------------------
⚠️ An earlier revision of this list recorded ONLY missed licences. That was a real omission with
a direction: a missed licence is harmless, so a list of only-missed-licences reads as a safety
argument while saying nothing about the dangerous half. The FALSE-LICENCE class is therefore
listed FIRST.

FALSE LICENCES (the dangerous direction — a wrongly granted warrant ADMITS a weld past the
guard's third arm, and an admitted weld logs nothing on the guard's normal path):
* A NAME parenthesised after its TYPE PHRASE. ⚠️ THIS BULLET USED TO READ "It is now refused —
  20 of 20 constructed cases", WHICH READS AS CLASS-CLOSED AND IS NOT TRUE. Both numbers in that
  sentence are real but they are measured on the SAME 20 nicknames, all of which happen to be
  prefix-run shaped. The honest statement is two-part:
    – PREFIX-RUN nicknames ({goldie} ≡ {golden retriever}): 11 of 20 licensed before the
      constraint, 0 of 20 after. CLOSED.
    – INITIALISM-SHAPED nicknames ({gr} ≡ {golden retriever}): 16 of those same 20 long forms
      are licensed END TO END, 18 of 20 at the function level. FULLY OPEN, and LARGER than the
      closed half. The two that fail are protected by word-count arithmetic, not by any
      recognition of a name — hyphenating the type restores the bypass.
  See "WHAT THIS CONSTRAINT DOES NOT CLOSE" above for the full measurement and the mechanism.
* Spurious pairs from a bracketed ASIDE that is not a definition at all. Measured across 5 443
  production turns: 3 of 12 pairs were this shape ({free}≡{freemium does not count},
  {cotton}≡{content may vary for different colors}, {online}≡{on reliability and security using
  best practices}). All 3 are killed by the initialism constraint; the corpus survey is now 7
  pairs, all genuine definitions.
* A TRUNCATED long form licensed INSTEAD of the one the sentence defines. "Oracle E-Business
  Suite (EBS)" emits {ebs} ≡ {e-business suite}, not {ebs} ≡ {oracle e-business suite}: the
  hyphen makes ``e`` and ``b`` word-initial, so the backward walk satisfies the constraint before
  it ever reaches "oracle". The pair emitted is a co-reference claim about a span the user did
  not define, and the pair the user DID define receives no warrant. See CALIBRATION item 3.
* The paper RECOMMENDS a corpus-level redundancy check as a further precision measure — this
  port OMITS it and scores each turn in isolation. That check is what a shipped system would use
  to discard a one-off pair that never recurs, and its absence is part of why the constructed
  false-licence class above matters more here than in the paper's setting.

MISSED LICENCES (the safe direction — the alias simply keeps today's unwarranted provenance):
* Only parenthetical definitions are recognised. "SAF stands for sustainable aviation fuel" and
  "sustainable aviation fuel, or SAF," carry a real licence this method does not see.
* Non-initial abbreviations (truncations/contractions) are refused by design — see WHAT IT COSTS
  above. Measured on the production corpus this removed {theo}≡{theodore} and the already-imprecise
  {cli}≡{command line tool}.
* REPEATED-LETTER TRUNCATION, inherited from the paper's backward walk and not introduced here:
  a short form whose letter recurs inside a long-form word matches the INNER occurrence first, so
  "registered retirement savings plan (RRSP)" yields the span "retirement savings plan" — which
  then fails the initialism test (one word-initial ``r``, two needed) and no licence is granted.
  The pair that was licensed before this constraint was the TRUNCATED, wrong long form.
* Under pattern (ii) this port takes exactly ONE word before the parenthesis as the short form,
  while §3.1 admits short forms "of at most two words". Widening it would grant MORE licences,
  which is the unsafe direction, so it is documented rather than changed.
* SENTENCE SEGMENTATION IS THE PORT'S OWN. The paper says only that the long form must "appear in
  the same sentence" and specifies no boundary test. See ``_is_sentence_end`` for the rules and
  for the two measured failures of the first cut. Measured effect of that fix on 5 443 production
  turns: ZERO pairs added or removed — it is a capability fix (it recovers "…heat shock;
  transcription factor (HSF)…" and "the U.S. Digital Service (USDS)"), not a widening.

CALIBRATION:
* The paper reports its precision on biomedical abstracts. This engine's text is conversational.
  The figure is quoted as the authors' result on their corpus, NOT claimed for this corpus. This
  port's own measured precision on 5 443 production turns is 7/7 pairs genuine (it was 9/12
  before the initialism constraint).
* ⚠️ RECALL IS **NOT** 100%, AND THE 7/7 ABOVE MUST NOT BE READ AS IF IT WERE. That figure is a
  PRECISION measure (of the pairs emitted, how many are genuine) over pairs this matcher happened
  to find, and the genuine-definition test set it is paired with was chosen by the same author as
  the matcher. Measured on 12 must-admit definitions chosen INDEPENDENTLY of that set,
  **8 of 12 are licensed — ~67% recall**, by four distinct mechanisms, ALL of which fail SAFE
  (no licence granted → the alias keeps today's unwarranted provenance):
    1. {rrsp} ≡ {registered retirement savings plan} — REPEATED-LETTER TRUNCATION (below). The
       backward walk matches the inner ``r`` of "retirement" first, yielding the truncated span
       "retirement savings plan", which then has only three word-initials (r,s,p) for four
       characters. Already disclosed under MISSED LICENCES; this is its independent confirmation.
    2. {u of t} ≡ {university of toronto} — a THREE-WORD short form, rejected by
       ``is_valid_short_form``'s ``_MAX_SHORT_WORDS = 2``. That cap is FAITHFUL to §3.1 ("at most
       two words"), so this is the paper's limit correctly implemented, not a port defect — but
       it is a real recall cost and was not previously listed as one.
    3. {ebs} ≡ {oracle e-business suite} — THE ONE WORTH READING TWICE. It does not simply miss:
       it SILENTLY LICENSES A DIFFERENT, TRUNCATED PAIR. ``find_best_long_form`` returns
       "e-business suite" (the hyphen makes ``e`` and ``b`` word-initial, so ``ebs`` aligns
       without ever reaching "oracle"), so the warrant is recorded for {ebs} ≡ {e-business suite}
       while the edge's ACTUAL surfaces — {ebs} and {oracle e-business suite} — get nothing. A
       caller asking ``text_licenses_coreference(text, 'oracle e-business suite', 'ebs')`` gets
       False even though the sentence plainly defines it. Still fail-safe for the WARRANT (the
       real pair is unwarranted, i.e. today's behaviour), but the emitted pair is a co-reference
       claim about a span the user did not define, so it is listed under FALSE LICENCES too.
    4. {nsaid} ≡ {nonsteroidal anti-inflammatory drug} — the ``s`` comes from "non**S**teroidal",
       an INTERIOR letter. This is the already-disclosed non-initial trade (see WHAT IT COSTS)
       working exactly as designed, and it is the same shape as the paper's own "GNAT"
       counterexample. Listed because it shows that trade is not hypothetical: NSAID is an
       extremely common real acronym and this constraint refuses it.
  Two of the four (1, 4) were already disclosed; two (2, 3) were not. Replace any "100% genuine"
  or "7/7" framing that implies recall with the independently-measured **8/12**.
"""
from __future__ import annotations

# Sentence terminators. ORTHOGRAPHY, not vocabulary — the paper scopes a long-form candidate to
# "the same sentence as the short form" and says NOTHING about how to find a sentence end, so
# everything below is THE PORT'S OWN and is marked as such.
#
# ⚠️ MEASURED FAILURES OF THE FIRST CUT, which was the flat set ".!?;\n\r":
#   * ";" is not a sentence terminator — a semicolon separates clauses WITHIN one sentence. It
#     split "…heat shock; transcription factor (HSF)…" and the pair was lost outright.
#   * A bare "." split INSIDE a dotted initialism: "the U.S. Food and Drug Administration (FDA)"
#     broke at "U.S." and amputated the long form.
# Both errors LOSE licences (the safe direction), which is exactly why neither showed up in a
# refusal-set measurement. They are fixed here rather than documented because the fix is itself
# purely orthographic and narrows nothing.
_ALWAYS_TERMINATES = "!?\n\r"
_PERIOD = "."


def _is_sentence_end(text, i) -> bool:
    """Is ``text[i]`` a SENTENCE boundary? Orthographic only — no lexicon, no abbreviation list.

    A period is NOT a sentence end when it is an ABBREVIATION DOT, detected by two shape rules
    that between them cover the dotted-initialism form ("U.S.", "e.g.", "Ph.D."):
      (a) it is followed immediately by an alphanumeric character — no space, so the token has
          not ended ("U.S" in "U.S."); and
      (b) it closes a letter-dot run — the preceding character is alphanumeric and the one
          before THAT is itself a period ("…S." in "U.S.").
    Everything else that is in ``_ALWAYS_TERMINATES`` or is a period terminates.

    Under-splitting is bounded and cannot run away: the §3.1 word cap ``min(|A|+5, |A|*2)`` still
    truncates the long-form window, and the initialism constraint still has to hold.
    """
    ch = text[i]
    if ch in _ALWAYS_TERMINATES:
        return True
    if ch != _PERIOD:
        return False
    if i + 1 < len(text) and text[i + 1].isalnum():
        return False                                    # (a) token has not ended
    if i >= 2 and text[i - 1].isalnum() and text[i - 2] == _PERIOD:
        return False                                    # (b) closes a letter-dot run
    return True

# The two bracket characters the paper's candidate extraction is DEFINED on ("Abbreviation
# candidates are determined by adjacency to parentheses"). Not a lexicon.
_OPEN, _CLOSE = "(", ")"

_MAX_SHORT_CHARS = 10          # §3.1 "their length is between two to ten characters"
_MIN_SHORT_CHARS = 2
_MAX_SHORT_WORDS = 2           # §3.1 "at most two words"
_PATTERN_II_INNER_WORDS = 2    # §3.1 "more than two words … pattern (ii) is assumed"


def _norm(surface):
    """Whitespace/case normalisation for comparing two surfaces. NOT a similarity measure."""
    return " ".join(str(surface or "").split()).strip().lower()


def is_valid_short_form(candidate) -> bool:
    """§3.1's validity test for a short-form candidate. Exact, no scoring."""
    s = str(candidate or "").strip()
    if not s:
        return False
    if len(s.split()) > _MAX_SHORT_WORDS:
        return False
    if not (_MIN_SHORT_CHARS <= len(s) <= _MAX_SHORT_CHARS):
        return False
    if not any(c.isalpha() for c in s):
        return False
    return s[0].isalnum()


def find_best_long_form(short_form: str, long_form: str):
    """Figure 1 (§3.2), ported. Returns the best long form, or ``None`` if no match.

    Ported statement-for-statement from the paper's Java listing. ⚠️ An earlier version of this
    docstring said "two Python notes, neither a behaviour change". There are FOUR edits, and two
    of them ARE behaviour changes on empty/``None`` input. Enumerated so a reader can audit the
    port against the listing instead of taking a summary's word for it:

      1. ``str.rfind`` replaces ``String.lastIndexOf``. NOT a behaviour change — both return -1
         when the needle is absent and the listing's ``+ 1`` then yields index 0, the intended
         "beginning of the long form".
      2. ``str.isalnum`` replaces ``Character.isLetterOrDigit``. NOT a behaviour change for the
         ASCII range the rule is defined on. (Python's ``isalnum`` is Unicode-wide and Java's
         method is too, so they also agree beyond it.)
      3. ``if short_form is None or long_form is None: return None`` — ADDED. The listing would
         raise a ``NullPointerException``; this returns "no match". A BEHAVIOUR CHANGE.
      4. ``if s_index < 0 or l_index < 0: return None`` — ADDED, guarding empty input. On an
         empty short form the listing's loop body never runs and it returns a substring (i.e.
         something, not null); this returns ``None``. A BEHAVIOUR CHANGE.

    Both changed behaviours are unreachable from this module's own call site (``is_valid_short_form``
    has already rejected empty input), but this function is PUBLIC and a caller may reach them,
    so they are stated rather than dismissed. Both resolve toward "no match", the safe direction.
    """
    if short_form is None or long_form is None:
        return None
    s_index = len(short_form) - 1
    l_index = len(long_form) - 1
    if s_index < 0 or l_index < 0:
        return None

    while s_index >= 0:
        curr_char = short_form[s_index].lower()
        # "ignore non alphanumeric characters"
        if not curr_char.isalnum():
            s_index -= 1
            continue
        # "Decrease lIndex while current character in the long form does not match … If the
        #  current character is the first character in the short form, decrement lIndex until
        #  a matching character is found at the beginning of a word in the long form."
        while ((l_index >= 0 and long_form[l_index].lower() != curr_char)
               or (s_index == 0 and l_index > 0 and long_form[l_index - 1].isalnum())):
            l_index -= 1
        if l_index < 0:
            return None
        l_index -= 1
        s_index -= 1

    # "Find the beginning of the first word (in case the first character matches the beginning
    #  of a hyphenated word)."
    # ``lastIndexOf(" ", lIndex)`` == last space at an index <= lIndex. Python's half-open
    # ``rfind(" ", 0, l_index + 1)`` is the same span, and at l_index == -1 both yield -1 → 0.
    # (An earlier version wrote ``max(l_index, 0) + 1``, which widened that window by one
    # character at l_index == -1 — a silent deviation from the listing, removed.)
    l_index = long_form.rfind(" ", 0, l_index + 1) + 1
    return long_form[l_index:]


def _word_key(word):
    """A word with its non-alphanumeric edges trimmed, for the §3.2 discard rule.

    The paper's rule speaks of the short form appearing "as one of the words in the long
    form". A word carrying its own brackets or comma — ``"(online),"`` — IS that word; reading
    it otherwise let a bracketed aside survive as a long form on real production text (4 of the
    7 spurious pairs this matcher produced across 5 443 turns were exactly that shape). Trimming
    is exact character removal, not normalisation-by-similarity.
    """
    return _norm(str(word).strip("".join(c for c in str(word) if not c.isalnum())) or word)


def _long_form_is_admissible(short_form: str, long_form: str) -> bool:
    """§3.2's prose rule, which the paper notes is "omitted from Figure 1"."""
    if len(long_form) < len(short_form):
        return False
    sf = _norm(short_form)
    return sf not in {_word_key(w) for w in long_form.split()}


def _word_initial_chars(long_form: str):
    """The first character of each orthographic WORD of ``long_form``, lowercased, in order.

    "Beginning of a word" is the paper's OWN boundary test, reused rather than reinvented: §3.2
    finds it by the PRECEDING character not being alphanumeric. That definition also makes each
    part of a hyphenated word initial ("X-ray" → x, r), which is the behaviour the listing's own
    comment says it wants. Orthographic; no tokeniser, no lexicon, no language assumption beyond
    "a non-alphanumeric character separates words".
    """
    return [ch.lower() for i, ch in enumerate(long_form)
            if ch.isalnum() and (i == 0 or not long_form[i - 1].isalnum())]


def is_leading_letter_match(short_form, long_form) -> bool:
    """LEADING-LETTER MATCHING — every character of the short form is WORD-INITIAL in the long form.

    NOT Schwartz & Hearst's rule (they constrain only the FIRST character), and not invented here
    either: this is the FIRST SENTENCE of Larkey et al.'s "**Lowercase strict**" scheme — "each
    letter in the acronym must be represented, in order, by the first letter of a word in the
    expansion" (Acrophile, Proc. ACM DL '00, pp. 205-214) — and Park & Byrd's formation code 'F',
    "the first character of a word occurs in the abbreviation" (EMNLP 2001, §3.1).

    ⚠️ THE FIRST SENTENCE ONLY — this function is NOT the whole of Larkey's scheme 2, and an
    earlier docstring said it was ("verbatim"). The scheme's second sentence, "The expansion must
    begin with the first letter of the acronym and must not contain uppercase letters", is not
    implemented here: the begins-with clause is satisfied structurally by
    ``find_best_long_form``'s span trimming in the pipeline, and the uppercase clause is
    inapplicable because ``_norm`` lowercases both surfaces. STANDALONE THIS FUNCTION IS
    THEREFORE LOOSER THAN THE SCHEME — ``is_leading_letter_match('saf', 'the sustainable aviation
    fuel')`` is True and Larkey's sentence 2 rejects it. That matters for direct callers (the
    test suite is one); it does not change what ``licensed_pairs`` emits. Full account, with the
    measurements, in the module docstring under WHERE THIS RULE COMES FROM.

    It is the constraint that separates an abbreviation definition from a NAME parenthesised after
    its TYPE PHRASE, two constructions Schwartz & Hearst cannot tell apart. See the module
    docstring for the measurement, for the paper's own explicit advice AGAINST adding constraints,
    and for exactly what this costs (non-initial truncations such as {config}≡{configuration} are
    refused along with {theo}≡{theodore}; they are the same orthographic object).

    Non-alphanumeric characters in the short form are skipped, matching §3.2's own "ignore non
    alphanumeric characters", so a dotted initialism ("S.A.F.") behaves as its letters do.

    Greedy left-to-right is exact here, not an approximation: this is an ordered-subsequence
    test over a FIXED sequence of positions, for which taking the earliest admissible match at
    every step never rules out a solution that exists.

    Fail-safe: anything that is not a non-empty run of characters yields False (no licence).
    """
    initials = _word_initial_chars(str(long_form or ""))
    consumed = 0
    j = 0
    for ch in str(short_form or "").lower():
        if not ch.isalnum():
            continue
        while j < len(initials) and initials[j] != ch:
            j += 1
        if j >= len(initials):
            return False
        j += 1
        consumed += 1
    return consumed > 0


def _sentences(text: str):
    """Split on sentence terminators so a long-form candidate cannot cross a sentence."""
    out, buf = [], []
    for i, ch in enumerate(text):
        if _is_sentence_end(text, i):
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _pairs_in_sentence(sentence: str):
    """Yield ``(short_form, long_form)`` definitions found in ONE sentence."""
    found = []
    pos = 0
    while True:
        open_at = sentence.find(_OPEN, pos)
        if open_at < 0:
            break
        close_at = sentence.find(_CLOSE, open_at + 1)
        if close_at < 0:
            break
        pos = close_at + 1
        inner = sentence[open_at + 1:close_at].strip()
        before = sentence[:open_at]
        if not inner:
            continue

        pattern_ii = len(inner.split()) > _PATTERN_II_INNER_WORDS
        if pattern_ii:
            # PATTERN (ii): short form '(' long form ')'. "a short form is searched for just
            # before the left parenthesis (word boundaries are indicated by spaces)."
            words_before = before.split()
            if not words_before:
                continue
            short_form, long_candidate = words_before[-1], inner
        else:
            # PATTERN (i): long form '(' short form ')'.
            short_form, long_candidate = inner, before

        if not is_valid_short_form(short_form):
            continue

        # "no more than min(|A| + 5, |A| * 2) words, where |A| is the number of characters in
        # the short form", "contiguous words … that include the word just before the short
        # form". The two patterns need the limit applied DIFFERENTLY, and conflating them is a
        # real (if silent) infidelity: under pattern (i) the candidate runs BACKWARD from the
        # parenthesis, so an over-long one is TRUNCATED to the words adjacent to the short
        # form; under pattern (ii) the candidate IS the parenthetical and has a fixed start, so
        # keeping only its tail would silently test a different string than the user wrote —
        # an over-long one is REJECTED instead.
        n = len(short_form)
        max_words = min(n + 5, n * 2)
        words = long_candidate.split()
        if not words:
            continue
        if len(words) > max_words:
            if pattern_ii:
                continue
            words = words[-max_words:]
        window = " ".join(words)

        best = find_best_long_form(short_form, window)
        if not best:
            continue
        best = best.strip()
        if not best or not _long_form_is_admissible(short_form, best):
            continue
        # ── OUR CONSTRAINT, applied AFTER the faithful port and never inside it. Schwartz &
        # Hearst license a NAME parenthesised after its TYPE PHRASE ("golden retriever
        # (Goldie)") because they constrain only the FIRST short-form character to a word
        # boundary. Requiring EVERY character to be word-initial is a strict subset of what the
        # paper admits, so it can only ever WITHHOLD a warrant. See the module docstring.
        if not is_leading_letter_match(short_form, best):
            continue
        found.append((short_form, best))
    return found


def licensed_pairs(text):
    """Return the set of co-reference pairs the TEXT ITSELF licenses.

    Each element is a ``frozenset({surface_a, surface_b})`` of normalised surfaces, so a caller
    can ask the orientation-free question "does this text license welding these two surfaces
    together?" without re-deriving which one is the abbreviation.

    Fail-safe: any malformed input yields an empty set (no licence granted → today's
    behaviour).
    """
    if not text:
        return frozenset()
    out = set()
    try:
        for sentence in _sentences(str(text)):
            for short_form, long_form in _pairs_in_sentence(sentence):
                a, b = _norm(short_form), _norm(long_form)
                if a and b and a != b:
                    out.add(frozenset((a, b)))
    except Exception:  # noqa: BLE001 — a licence is never granted on our own error
        return frozenset()
    return frozenset(out)


def text_licenses_coreference(text, surface_a, surface_b) -> bool:
    """Does ``text`` contain an abbreviation definition binding these two surfaces?

    Orientation-free and exact (normalised string equality against the extracted pair) — this
    is deliberately NOT a containment, prefix or similarity test.
    """
    a, b = _norm(surface_a), _norm(surface_b)
    if not a or not b or a == b:
        return False
    return frozenset((a, b)) in licensed_pairs(text)
