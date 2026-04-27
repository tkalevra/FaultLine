"""
title: FaultLine WGM Filter
author: tkalevra
version: 1.0.0
required_open_webui_version: 0.9.0
requirements: httpx
"""

import asyncio
import json
from typing import Optional

import httpx
from pydantic import BaseModel


class Filter:
    class Valves(BaseModel):
        FAULTLINE_URL: str = "http://192.168.40.10:8001"
        FAULTLINE_TIMEOUT: int = 20
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

    def _normalize_edges(self, edges: list[dict]) -> list[dict]:
        return [
            {
                "subject": e.get("subject", "").lower().strip(),
                "object": e.get("object", "").lower().strip(),
                "rel_type": e.get("rel_type", "").lower().strip(),
            }
            for e in edges
            if e.get("subject") and e.get("object") and e.get("rel_type")
        ]

    async def _fire_ingest(self, text: str, source: str, user_id: str = "anonymous") -> None:
        try:
            async with httpx.AsyncClient(
                timeout=self.valves.FAULTLINE_TIMEOUT
            ) as client:
                await client.post(
                    f"{self.valves.FAULTLINE_URL}/ingest",
                    json={
                        "text": text,
                        "source": source,
                        "edges": [],
                        "user_id": user_id,
                    },
                )
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] ingest error: {e}")

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not self.valves.ENABLED or not self.valves.INGEST_ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "user")
            if text:
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] inlet firing ingest: {text[:80]}")
                user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"
                asyncio.create_task(self._fire_ingest(text, self.valves.DEFAULT_SOURCE, user_id))
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet error: {e}")

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        if not self.valves.ENABLED or not self.valves.QUERY_ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "assistant")
            if not text:
                return body

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet querying: {text[:80]}")

            user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"
            async with httpx.AsyncClient(
                timeout=self.valves.FAULTLINE_TIMEOUT
            ) as client:
                response = await client.post(
                    f"{self.valves.FAULTLINE_URL}/query",
                    json={
                        "text": text,
                        "source": self.valves.DEFAULT_SOURCE,
                        "user_id": user_id,
                    },
                )

            if response.status_code != 200:
                return body

            data = response.json()
            facts = data.get("facts", [])

            if not facts:
                return body

            fact_lines = "\n".join(
                f"- {f.get('subject')} {f.get('rel_type')} {f.get('object')} ({f.get('status', 'valid')})"
                for f in facts
            )

            memory_block = f"\n\n---\n🧠 **Memory context:**\n{fact_lines}"

            messages = body.get("messages", [])
            for i in reversed(range(len(messages))):
                if messages[i].get("role") == "assistant":
                    messages[i]["content"] = messages[i]["content"] + memory_block
                    break

        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet error: {e}")

        return body
