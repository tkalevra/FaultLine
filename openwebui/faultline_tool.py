"""
title: FaultLine WGM Filter
author: tkalevra
version: 1.2.0
required_open_webui_version: 0.9.0
requirements: httpx
"""

import asyncio
import json
import os
import re
from typing import Optional

import httpx
from pydantic import BaseModel


_TRIPLE_SYSTEM_PROMPT = """\
You are a relationship fact extractor for a personal knowledge graph.
Output ONLY a raw JSON array. No markdown, no explanation, no code fences.

RULES:

ENTITY NAMING RULES (strictly enforced):
- NEVER use "i", "me", "my", "we", "our", "myself" as subject or object in ANY triple regardless of rel_type. This is an absolute rule with zero exceptions.
- If the subject of a fact is ambiguous due to pronouns, resolve it to the nearest named entity in the sentence. For "Marla, who prefers to be called Mars", the subject is "marla" not "i".
- For preference patterns ("X prefers Y", "X goes by Y", "X is called Y"), the subject is always the person being described, never the speaker.
- Entity names must be proper nouns or named entities only. Never common nouns, pronouns, or role labels (e.g. not "user", "person", "speaker").

RELATIONSHIP RULES (strictly enforced):
- "child_of" means the subject IS the child, the object IS the parent. Example: "henry child_of thomas" means henry is thomas's child.
- "parent_of" means the subject IS the parent, the object IS the child. Example: "thomas parent_of henry" means thomas is henry's parent.
- Never emit child_of or parent_of between two siblings. If two entities share the same parent, emit sibling_of between them instead.
- For "X and Y are children of Z", emit three triples: Z parent_of X, Z parent_of Y, X sibling_of Y.
- Directionality is absolute. When in doubt, prefer parent_of with the parent as subject.
- NEVER emit child_of where the subject is the person speaking or the established user identity. If the user says "I have children" or "we have children", emit parent_of with the user as subject, never child_of with the user as subject.

1. Extract ALL factual assertions — people, animals, objects, places, preferences.
2. Entities must be specific names or nouns. Lowercase all values.
3. rel_type must be snake_case. Use the most precise label that fits.
   Common types: is_a, has_pet, parent_of, child_of, spouse, sibling_of,
   also_known_as, works_for, likes, dislikes, prefers, lives_at, owns.
   You may use other snake_case types if none of the above fit.
4. also_known_as = nickname or alias ONLY (e.g. "Theo" for "Theodore").
5. is_a = type or category (e.g. "morkie is_a dog breed").
6. has_pet = person owns an animal (e.g. "we have a dog named Biskit").
7. For "X is a Y" patterns use is_a. For "named X" patterns use also_known_as
   between the descriptor and the name.
8. Pronoun resolution: replace he/she/it with the named entity if clear.
9. If unsure, set "low_confidence": true. Never return empty if facts exist.
10. First-person "my/our/we/I/me" refers to the named user entity if established,
    otherwise use subject "user".
11. If the message contains a self-identification pattern ("I am X", "I'm X",
    "my name is X", "call me X"), emit an also_known_as triple:
    {"subject":"user","object":"x","rel_type":"also_known_as","low_confidence":false}
    where X is the proper name, lowercased.
15. CORRECTION DETECTION: If the message indicates a prior fact was wrong
    ("actually", "his name is", "it's supposed to be", "I meant",
    "not X, it's Y", "correct that to"), extract the correction as a new
    triple with the corrected value. Use prior context to resolve the
    subject. Mark corrected triples with "is_correction": true.
    e.g. "oh his name is actually Biscuit" (context: dog named Biskit) →
    {"subject":"biskit","object":"biscuit","rel_type":"also_known_as",
     "is_correction":true,"low_confidence":false}

OUTPUT: [{"subject":"...","object":"...","rel_type":"...","low_confidence":false}]
If nothing to extract: []"""


async def rewrite_to_triples(text: str, valves, context: list[dict] = None, typed_entities: list[dict] = None) -> list[dict]:
    """
    Send text to the Qwen model and parse the returned JSON triple array.
    Context (prior messages) provides conversation history for resolution.
    Returns [] on any failure so the caller can handle the empty-edge case.
    """
    try:
        messages = [{"role": "system", "content": _TRIPLE_SYSTEM_PROMPT}]

        if context:
            prior_turns = []
            for msg in context[-6:]:
                role = msg.get("role")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "") for p in content if isinstance(p, dict)
                        )
                    if content.strip():
                        prior_turns.append({"role": role, "content": content})
            # Include all prior turns except the current message (last user turn)
            # which is already appended as the final message separately
            if prior_turns:
                # Drop the last user turn from context since it's the current message
                turns_to_add = prior_turns[:-1] if (
                    prior_turns[-1]["role"] == "user"
                ) else prior_turns
                messages.extend(turns_to_add)

        user_content = text
        if typed_entities:
            entity_lines = "\n".join(
                f"- {e.get('subject')} (type: {e.get('subject_type', 'unknown')})"
                f" {e.get('rel_type')} {e.get('object')} (type: {e.get('object_type', 'unknown')})"
                for e in typed_entities
                if e.get("subject") and e.get("object")
            )
            user_content = (
                f"{text}\n\n"
                f"GLiNER2 has pre-classified these entities from the text:\n{entity_lines}\n"
                f"Use these entity types to guide relationship selection. "
                f"A Person cannot be owned. An Animal cannot be a spouse. "
                f"Respect these types strictly."
            )
        messages.append({"role": "user", "content": user_content})

        async with httpx.AsyncClient(timeout=valves.QWEN_TIMEOUT) as client:
            response = await client.post(
                valves.QWEN_URL,
                json={
                    "model": valves.QWEN_MODEL,
                    "messages": messages,
                    "temperature": 0.0,
                    "max_tokens": 400,
                    "thinking": {"type": "disabled"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            triples = json.loads(content)
            if not isinstance(triples, list):
                return []
            return triples
    except httpx.HTTPStatusError as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples HTTP error: {e.response.status_code}")
            print(f"[FaultLine] rewrite_to_triples response body: {e.response.text}")
        return []
    except Exception as e:
        if valves.ENABLE_DEBUG:
            print(f"[FaultLine] rewrite_to_triples failed: {e}")
        return []


class Filter:
    """
    OpenWebUI Filter for FaultLine WGM Integration.

    inlet:  extract and commit facts from user messages (write path, fire-and-forget)
    outlet: query Qdrant for relevant memories and append to response (read-only)
    """

    class Valves(BaseModel):
        FAULTLINE_URL: str = "http://192.168.40.10:8001"
        FAULTLINE_TIMEOUT: int = 30
        QWEN_URL: str = os.getenv("QWEN_URL", "http://192.168.40.20:1234/v1/chat/completions")
        QWEN_MODEL: str = "qwen/qwen3.5-9b"
        QWEN_TIMEOUT: int = 10
        DEFAULT_SOURCE: str = "openwebui"
        ENABLE_DEBUG: bool = False
        ENABLED: bool = True
        INGEST_ENABLED: bool = True
        QUERY_ENABLED: bool = True

    def __init__(self):
        self.valves = self.Valves()

    def _last_message(self, messages: list, role: str) -> Optional[str]:
        for m in reversed(messages):
            if m.get("role") == role:
                return m.get("content", "")
        return None

    async def _fire_ingest(
        self,
        text: str,
        source: str,
        user_id: str = "anonymous",
        edges: Optional[list[dict]] = None,
    ) -> dict:
        try:
            payload = {
                "text": text,
                "source": source,
                "user_id": user_id,
                "known_types": ["Person", "Organization", "Location", "Event", "Concept"],
            }
            if edges:
                payload["edges"] = edges

            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                response = await client.post(
                    f"{self.valves.FAULTLINE_URL}/ingest",
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except httpx.ConnectError as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] Connection error: {e}")
            return {"status": "error", "detail": "FaultLine unreachable"}
        except httpx.TimeoutException as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] Timeout: {e}")
            return {"status": "error", "detail": "FaultLine timeout"}
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] Ingest error: {e}")
            return {"status": "error", "detail": str(e)}

    async def _fetch_entities(self, text: str, user_id: str) -> list[dict]:
        """Call /extract to get GLiNER2-typed entities before Qwen runs."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.valves.FAULTLINE_URL}/extract",
                    json={"text": text, "source": "preflight", "user_id": user_id},
                )
            if resp.status_code == 200:
                return resp.json().get("entities", [])
        except Exception:
            pass
        return []

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        """
        Rewrite the user's message to structured triples via Qwen, fire-and-forget
        ingest, then query FaultLine for relevant memories and inject them into
        the user message before the model sees it.
        """
        if not self.valves.ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "user")
            if not text:
                return body

            user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"

            _IDENTITY_RE = re.compile(
                r"\b(my name is|i am|i'm|call me|people call me)\s+[a-z]+", re.IGNORECASE
            )
            will_ingest = self.valves.INGEST_ENABLED and (
                len(text.split()) >= 5 or bool(_IDENTITY_RE.search(text))
            )
            will_query = self.valves.QUERY_ENABLED

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet text='{text[:80]}' will_ingest={will_ingest}")

            if not will_ingest and not will_query:
                return body

            if will_ingest:
                # Strip injected memory block before extraction so Qwen
                # doesn't extract facts from our own injections
                _MEMORY_MARKER = "\n\n🧠 Memory context from FaultLine:"
                clean_text = text.split(_MEMORY_MARKER)[0].strip() if _MEMORY_MARKER in text else text

                typed_entities = await self._fetch_entities(clean_text, user_id)
                raw_triples = await rewrite_to_triples(
                    clean_text, self.valves, context=body.get("messages", []),
                    typed_entities=typed_entities if typed_entities else None
                )
                confident = [e for e in raw_triples if not e.get("low_confidence", False)]
                edges = [
                    {
                        "subject": e["subject"],
                        "object": e["object"],
                        "rel_type": e["rel_type"],
                        "is_preferred_label": e.get("is_preferred_label", False),
                        "is_correction": e.get("is_correction", False),
                    }
                    for e in confident
                    if e.get("subject") and e.get("object") and e.get("rel_type")
                ]
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] inlet firing ingest: {text[:80]} edges={len(edges)}")
                asyncio.create_task(
                    self._fire_ingest(
                        clean_text,
                        self.valves.DEFAULT_SOURCE,
                        user_id,
                        edges=edges,
                    )
                )

            if will_query:
                try:
                    async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                        resp = await client.post(
                            f"{self.valves.FAULTLINE_URL}/query",
                            json={"text": text, "user_id": user_id, "top_k": 5},
                        )
                    if resp.status_code == 200:
                        facts = resp.json().get("facts", [])
                        preferred_names = resp.json().get("preferred_names", {})
                        if facts or preferred_names:
                            if self.valves.ENABLE_DEBUG:
                                print(f"[FaultLine Filter] inlet injecting {len(facts)} facts + {len(preferred_names)} preferred names")
                            identity = preferred_names.get("user") or next(
                                (f.get("object") for f in facts
                                 if f.get("subject") == "user" and f.get("rel_type") == "also_known_as"),
                                None,
                            )
                            if identity:
                                identity_line = (
                                    f"The user in this conversation is '{identity}'. "
                                    f"When facts reference '{identity}', that means YOU (the user). "
                                    f"Do not describe '{identity}' as a third party.\n"
                                )
                            else:
                                identity_line = "Note: you are the user these facts refer to.\n"

                            # Inject identity anchor into system message for hard grounding
                            if identity:
                                system_anchor = (
                                    f"[Memory] The user's name is {identity}. "
                                    f"All facts referencing '{identity}' describe this user directly. "
                                    f"Never refer to {identity} as a third party or someone else."
                                )
                                messages = body.get("messages", [])
                                for i, msg in enumerate(messages):
                                    if msg.get("role") == "system":
                                        messages[i]["content"] = messages[i]["content"] + "\n\n" + system_anchor
                                        break
                                else:
                                    # No system message exists — prepend one
                                    messages.insert(0, {"role": "system", "content": system_anchor})

                            memory_lines = []
                            if preferred_names:
                                memory_lines.append("## Preferred Names (always use these, never the canonical form unless explicitly asked for a full or legal name)")
                                for canonical, preferred in preferred_names.items():
                                    if canonical != identity:
                                        memory_lines.append(f"- Always call {canonical.title()} by '{preferred.title()}'. Only use '{canonical.title()}' if asked for their full or legal name.")
                                memory_lines.append("")

                            if facts:
                                # Group facts by rel_type for natural sentence construction
                                from collections import defaultdict
                                by_rel = defaultdict(list)
                                for f in facts:
                                    by_rel[f.get("rel_type", "")].append(f)

                                sentences = []
                                # Build a nickname lookup from also_known_as facts: canonical → alias
                                # e.g. charles → chuck means display as "Charles (Chuck)"
                                nickname_map = {}
                                for f in by_rel.get("also_known_as", []):
                                    canonical = f.get("subject", "")
                                    alias = f.get("object", "")
                                    if canonical and alias and canonical != identity:
                                        nickname_map[canonical] = alias

                                def _display_name(name: str) -> str:
                                    """Return preferred name if one exists, otherwise canonical name."""
                                    if name in nickname_map:
                                        return nickname_map[name].title()
                                    return name.title()

                                children_raw = [f.get("object") for f in by_rel.get("parent_of", []) if identity and f.get("subject") == identity]
                                parents_raw = [f.get("object") for f in by_rel.get("child_of", []) if identity and f.get("subject") == identity]
                                spouses_raw = [f.get("object") for f in by_rel.get("spouse", []) if identity and (f.get("subject") == identity or f.get("object") == identity)]
                                spouses_raw += [f.get("subject") for f in by_rel.get("spouse", []) if identity and f.get("object") == identity and f.get("subject") not in spouses_raw]
                                siblings_raw = [f.get("object") for f in by_rel.get("sibling_of", []) if identity and f.get("subject") == identity]

                                children = [_display_name(c) for c in children_raw]
                                parents = [_display_name(p) for p in parents_raw]
                                spouses = [_display_name(s) for s in set(spouses_raw)]
                                siblings = [_display_name(s) for s in siblings_raw]

                                if children:
                                    sentences.append(f"You have {len(children)} {'child' if len(children) == 1 else 'children'}: {', '.join(children)}.")
                                if spouses:
                                    sentences.append(f"You are married to {', '.join(set(spouses))}.")
                                if parents:
                                    sentences.append(f"Your {'parent is' if len(parents) == 1 else 'parents are'} {', '.join(parents)}.")
                                if siblings:
                                    sentences.append(f"Your {'sibling is' if len(siblings) == 1 else 'siblings are'} {', '.join(siblings)}.")

                                # Render remaining facts not already covered
                                covered_rels = {"parent_of", "child_of", "spouse", "sibling_of", "also_known_as"}
                                for f in facts:
                                    if f.get("rel_type") in covered_rels:
                                        continue
                                    subj = f.get("subject", "")
                                    obj = f.get("object", "")
                                    rel = f.get("rel_type", "").replace("_", " ")
                                    if identity and subj == identity:
                                        sentences.append(f"You {rel} {obj}.")
                                    elif identity and obj == identity:
                                        sentences.append(f"{subj.title()} {rel} you.")
                                    else:
                                        sentences.append(f"{subj.title()} {rel} {obj}.")

                                if sentences:
                                    memory_lines.append("## What I know about you")
                                    for s in sentences:
                                        memory_lines.append(f"- {s}")

                            memory_block = (
                                f"\n\n🧠 Memory context from FaultLine:\n"
                                f"{identity_line}"
                                f"IMPORTANT: These facts are about the person you are speaking with directly. "
                                f"Do not invent parents, ancestors, or additional family members not listed here. "
                                f"Do not reframe the user as a child or sibling. Only state what the facts explicitly say.\n"
                                + "\n".join(memory_lines)
                            )
                            messages = body.get("messages", [])
                            for i in reversed(range(len(messages))):
                                if messages[i].get("role") == "user":
                                    messages[i]["content"] = messages[i]["content"] + memory_block
                                    break
                except Exception:
                    pass

        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet error: {e}")

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        return body
