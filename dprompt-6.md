# Deepseek Evaluation Prompt: Qwen Date/Time Extraction Robustness

**Scope:** Evaluate current Qwen prompt's DATES AND EVENTS section. Identify gaps, propose improvements, validate with test cases.

**Why:** NEXT_STEPS.md lists "expand date/time extraction" as High Priority #2 for usability. Birthdays, anniversaries, meeting dates are critical for memory recall. Current DATES AND EVENTS section (added May 6) may be incomplete or untested.

---

## Current State

**Qwen prompt DATES AND EVENTS section** (lines 138–166 in `openwebui/faultline_tool.py`):

```
DATES AND EVENTS:
- Birthday/birth date patterns ("my birthday is X", "born on X", "I was born on X", "born in X"):
  emit {"subject":"user","object":"<date>","rel_type":"born_on"} where object is the date as stated (e.g. "may 3", "march 15th", "1988-04-02"). Normalize to lowercase.
- Person's birthday ("Des's birthday is X", "my son's birthday is Y"):
  emit {"subject":"<entity>","object":"<date>","rel_type":"born_on"}. Example: "My daughter Emma was born on June 15" → (emma, born_on, "june 15").
- Anniversaries ("our anniversary is X", "our wedding anniversary is X"):
  emit {"subject":"user" (or both entities if named),"object":"<date>","rel_type":"anniversary_on"}.
- Meeting/first-encounter dates ("we met on X", "we first met on X"):
  emit {"subject":"user","object":"<entity>","rel_type":"met_on"} OR use met_on as the date event rel_type depending on context.
- Marriage/wedding dates ("we got married on X", "we were married on X"):
  emit {"subject":"user","object":"<date>","rel_type":"married_on"} OR emit spouse relationship separately.
- Relative date references ("next week", "last month", "in 3 weeks", "a month ago"):
  Emit the relative date as-is ("next week", "last month", "in 3 weeks") as the object. System will normalize these contextually.
- Date formats: month/day ("may 3rd", "december 25"), full dates ("march 15, 1990"), years ("born in 1988"), relative ("next thursday", "2 weeks ago").
- Date values must be the date string only — never a name or description.
```

**Status:** Section exists but untested. Unclear if LLM follows it correctly or if there are edge cases.

---

## Evaluation Questions

1. **Coverage:** Does the current section handle all common date/time patterns users would naturally express?
   - Birthdays: "I'm 25, born on May 3rd" (age + date)?
   - Anniversaries: "We've been together 5 years, anniversary is June 20" (duration + date)?
   - Fuzzy dates: "born sometime in 1990", "around May", "early June"?
   - Compound: "My son Des was born May 3, 1995" (name + date)?

2. **Clarity:** Are the instructions unambiguous? Could the LLM misinterpret any of them?
   - Example: "emit {"subject":"user" (or both entities if named)" — this is vague. Should it be one rule?
   - Example: "use met_on as the date event rel_type depending on context" — unclear what "depending on context" means

3. **Rel_type correctness:** Are the right rel_types used for each scenario?
   - `born_on`: birthdate (person → date) ✓
   - `anniversary_on`: anniversary date (person/couple → date) — is this defined in `rel_types` table?
   - `met_on`: first meeting (person → person) or (person → date)? Ambiguous.
   - `married_on`: wedding date (person/couple → date) — is this defined?

4. **Testing gaps:** What test cases would validate extraction is working?
   - Positive cases (should extract)
   - Negative cases (should NOT extract as date when it's narrative)
   - Edge cases (partial dates, ambiguous phrasing, corrections)

---

## What We Need From You

**Option A: Section is fine, just needs testing**
- List 10–15 test cases (inputs + expected extractions)
- Suggest how to validate in OpenWebUI (e.g., "tell system X, query Y, confirm memory contains Z")

**Option B: Section has gaps**
- List specific missing patterns
- Suggest rewrites or clarifications
- Propose new rel_types if needed

**Option C: Rel_type issue**
- Confirm `anniversary_on`, `met_on`, `married_on` are in the `rel_types` table
- If not, propose which existing rel_types to use instead

**Your call:** What's the state, and what's the next concrete step?

---

## Context: Why This Matters

Every time a user says "I was born on May 3rd" or "Our anniversary is June 20th", the system either:
- **Extracts it** → stored in `facts(born_on, anniversary_on, etc.)` → `/query` retrieves it → memory injects it → user asks "When was I born?" → LLM answers correctly
- **Fails to extract** → lost forever → user repeats themselves → friction

Date/time facts are the lowest-hanging fruit for immediate usability gain (relative to #1 test coverage, which is defensive).

---

## Done When

- ✅ Current section evaluated
- ✅ Gaps identified (if any)
- ✅ Test cases proposed (10–15)
- ✅ Rel_type alignment confirmed (annotation_on, etc. exist or use alternatives)
- ✅ Ready for manual validation in OpenWebUI

Ship it.

