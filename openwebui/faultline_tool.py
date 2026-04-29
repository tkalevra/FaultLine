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
from typing import Optional

import httpx
from pydantic import BaseModel


_TRIPLE_SYSTEM_PROMPT = """\
You are a relationship fact extractor for a personal knowledge graph.
Output ONLY a raw JSON array. No markdown, no explanation, no code fences.

RULES:
1. Extract ALL factual assertions — people, animals, objects, places, preferences.
2. Entities must be specific names or nouns. Lowercase all values.
3. rel_type must be snake_case. Use the most precise label that fits.
   Common types: is_a, has_pet, parent_of, child_of, spouse, sibling_of,
   also_known_as, works_for, likes, dislikes, prefers, lives_at, owns.
   You may use other snake_case types if none of the above fit.
4. also_known_as = nickname or alias ONLY (e.g. "Cy" for "Cyrus").
5. is_a = type or category (e.g. "morkie is_a dog breed").
6. has_pet = person owns an animal (e.g. "we have a dog named Fragglr").
7. For "X is a Y" patterns use is_a. For "named X" patterns use also_known_as
   between the descriptor and the name.
8. Pronoun resolution: replace he/she/it with the named entity if clear.
9. If unsure, set "low_confidence": true. Never return empty if facts exist.
10. First-person "my/our/we" refers to subject "user".
15. CORRECTION DETECTION: If the message indicates a prior fact was wrong
    ("actually", "his name is", "it's supposed to be", "I meant",
    "not X, it's Y", "correct that to"), extract the correction as a new
    triple with the corrected value. Use prior context to resolve the
    subject. Mark corrected triples with "is_correction": true.
    e.g. "oh his name is actually Fraggle" (context: dog named Fragglr) →
    {"subject":"fragglr","object":"fraggle","rel_type":"also_known_as",
     "is_correction":true,"low_confidence":false}

OUTPUT: [{"subject":"...","object":"...","rel_type":"...","low_confidence":false}]
If nothing to extract: []"""


async def _should_ingest(text: str, valves) -> bool:
    if len(text.split()) < 5:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                valves.QWEN_URL,
                json={
                    "model": valves.QWEN_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a memory-relevance classifier for a personal knowledge graph. "
                                "Reply with only the single word yes or no. "
                                "No punctuation, no explanation. "
                                "When in doubt, reply yes."
                            )
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Does this message contain a factual assertion, a correction to a prior "
                                f"fact, or any statement about a person, animal, place, or object that "
                                f"should be remembered? Reply with only: yes or no.\n\n\"{text}\""
                            )
                        }
                    ],
                    "temperature": 0.0,
                    "max_tokens": 5,
                    "thinking": {"type": "disabled"},
                },
            )
        return r.json()["choices"][0]["message"]["content"].strip().lower().startswith("yes")
    except Exception:
        return True


async def rewrite_to_triples(text: str, valves, context: list[dict] = None) -> list[dict]:
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
                if msg.get("role") in ("user", "assistant"):
                    prior_turns.append({
                        "role": msg["role"],
                        "content": msg.get("content", "")[:200]
                    })
            if len(prior_turns) > 1:
                messages.extend(prior_turns[:-1])

        messages.append({"role": "user", "content": text})

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

            will_ingest = self.valves.INGEST_ENABLED and await _should_ingest(text, self.valves)
            will_query = self.valves.QUERY_ENABLED

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet text='{text[:80]}' will_ingest={will_ingest}")

            if not will_ingest and not will_query:
                return body

            if will_ingest:
                raw_triples = await rewrite_to_triples(text, self.valves, context=body.get("messages", []))
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
                        text,
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
                            identity_line = (
                                f"You are {identity} in these facts.\n"
                                if identity
                                else "Note: you are the user these facts refer to.\n"
                            )

                            memory_lines = []
                            if preferred_names:
                                memory_lines.append(
                                    "IMPORTANT: Use ONLY the following names. "
                                    "Never use the alternate form in your response:"
                                )
                                for subject, preferred_obj in preferred_names.items():
                                    memory_lines.append(
                                        f"- Always refer to '{subject}' as '{preferred_obj}'"
                                    )
                                memory_lines.append("")

                            if facts:
                                memory_lines.append("## Facts")
                                for f in facts:
                                    subj = f.get("subject", "")
                                    rel = f.get("rel_type", "")
                                    obj = f.get("object", "")
                                    if rel == "also_known_as" and subj in preferred_names:
                                        continue
                                    display_subj = preferred_names.get(subj, subj)
                                    display_obj = preferred_names.get(obj, obj)
                                    memory_lines.append(f"- {display_subj} {rel} {display_obj}")

                            memory_block = f"\n\n🧠 Memory context from FaultLine:\n{identity_line}" + "\n".join(memory_lines)
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
