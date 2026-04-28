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
You are a relationship fact extractor. Output ONLY a raw JSON array, nothing else.
No thinking, no explanation, no markdown, no code fences, no preamble.

STRICT RULES:
1. Only extract relationships explicitly stated in the text.
2. Entities must be proper names only. No pronouns, no "the user", no generics.
3. All subject and object values must be lowercase.
4. rel_type must be EXACTLY one of: parent_of, child_of, spouse, sibling_of, also_known_as, works_for, likes, dislikes, prefers
5. also_known_as means nickname or alias ONLY — e.g. "Cyrus also known as Cy" → {"subject":"cyrus","object":"cy","rel_type":"also_known_as"}
6. parent_of means a parent-child relationship — e.g. "Christopher has a son Cyrus" → {"subject":"christopher","object":"cyrus","rel_type":"parent_of"}
7. spouse means married or partnered — e.g. "Christopher's spouse is Marla" → {"subject":"christopher","object":"marla","rel_type":"spouse"}
8. Never use also_known_as for a parent-child or spouse relationship.
9. If unsure, set "low_confidence": true. Never silently drop a relation.
10. If the input contains a first-person statement identifying the speaker's name (e.g. "My name is X", "I am X", "I'm X"), emit exactly one triple: {"subject":"user","object":"<name>","rel_type":"also_known_as","low_confidence":false}
11. For preference statements ("X likes Y", "X loves Y", "X enjoys Y", "X is into Y", "X prefers Y"), emit: {"subject":"<person>","object":"<thing>","rel_type":"likes"}. For negative preferences ("X hates Y", "X dislikes Y", "X doesn't like Y"), use rel_type "dislikes".
12. Pronoun resolution: if the input contains "she", "her", "he", "his", and a named person of that gender was mentioned earlier in the same input, replace the pronoun with that person's name when extracting triples. If ambiguous, set low_confidence: true.
13. "my wife", "my husband", "my son", "my daughter", "my spouse" always refers to the person linked to "user" via the spouse/parent_of relation in prior context. Use their name if known from the same input, otherwise set low_confidence: true.

OUTPUT FORMAT — exactly this, nothing else:
[{"subject":"...","object":"...","rel_type":"...","low_confidence":false}]"""


_INGEST_KEYWORDS = {
    "is", "are", "was", "were", "has", "have", "like", "likes", "loves",
    "enjoys", "hates", "knows", "works", "married", "son", "daughter",
    "wife", "husband", "parent", "child", "sibling", "brother", "sister",
    "nickname", "called",
}


async def rewrite_to_triples(text: str, valves) -> list[dict]:
    """
    Send text to the Qwen model and parse the returned JSON triple array.
    Returns [] on any failure so the caller can handle the empty-edge case.
    """
    try:
        async with httpx.AsyncClient(timeout=valves.QWEN_TIMEOUT) as client:
            response = await client.post(
                valves.QWEN_URL,
                json={
                    "model": valves.QWEN_MODEL,
                    "messages": [
                        {"role": "system", "content": _TRIPLE_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
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
        QWEN_MODEL: str = "qwen/qwen3.5-9b@q4_k_m"
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

            text_lower = text.lower()
            words = text_lower.split()
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet text='{text[:80]}' words={len(words)} keyword_match={bool(set(words) & _INGEST_KEYWORDS or 'also known' in text_lower)}")

            keyword_match = len(words) >= 5 and bool(
                set(words) & _INGEST_KEYWORDS or "also known" in text_lower
            )
            will_ingest = self.valves.INGEST_ENABLED and keyword_match
            will_query = self.valves.QUERY_ENABLED

            if not will_ingest and not will_query:
                return body

            if will_ingest:
                raw_triples = await rewrite_to_triples(text, self.valves)
                confident = [e for e in raw_triples if not e.get("low_confidence", False)]
                edges = [
                    {"subject": e["subject"], "object": e["object"], "rel_type": e["rel_type"]}
                    for e in confident
                    if e.get("subject") and e.get("object") and e.get("rel_type")
                ]
                if edges:
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
                        if facts:
                            if self.valves.ENABLE_DEBUG:
                                print(f"[FaultLine Filter] inlet injecting {len(facts)} facts into user message")
                            identity = next(
                                (f.get("object") for f in facts
                                 if f.get("subject") == "user" and f.get("rel_type") == "also_known_as"),
                                None,
                            )
                            identity_line = (
                                f"You are {identity} in these facts.\n"
                                if identity
                                else "Note: you are the user these facts refer to.\n"
                            )
                            fact_lines = "\n".join(
                                f"- {f.get('subject')} {f.get('rel_type')} {f.get('object')}"
                                for f in facts
                            )
                            memory_block = f"\n\n🧠 Memory context from FaultLine:\n{identity_line}{fact_lines}"
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
