"""
title: FaultLine WGM Filter
author: tkalevra
version: 1.2.0
required_open_webui_version: 0.9.0
requirements: httpx
"""

import asyncio
import json
from typing import Optional

import httpx
from pydantic import BaseModel


class Filter:
    """
    OpenWebUI Filter for FaultLine WGM Integration.

    inlet:  extract and commit facts from user messages (write path, fire-and-forget)
    outlet: query Qdrant for relevant memories and append to response (read-only)
    """

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
        Fire-and-forget ingest of the user's message into the WGM pipeline.
        Does not block the conversation flow.
        """
        if not self.valves.ENABLED or not self.valves.INGEST_ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "user")
            if text:
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] inlet firing ingest: {text[:80]}")
                user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"
                asyncio.create_task(
                    self._fire_ingest(
                        text,
                        self.valves.DEFAULT_SOURCE,
                        user_id,
                        edges=None,
                    )
                )
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] inlet error: {e}")

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
    ) -> dict:
        """
        Query Qdrant for facts relevant to the user's message and append them
        as a memory block below the assistant's response.
        """
        if not self.valves.ENABLED or not self.valves.QUERY_ENABLED:
            return body

        try:
            # Use the user's question as the retrieval query, not the assistant's response
            text = self._last_message(body.get("messages", []), "user")
            if not text:
                return body

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet querying: {text[:80]}")

            user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"

            async with httpx.AsyncClient(timeout=self.valves.FAULTLINE_TIMEOUT) as client:
                response = await client.post(
                    f"{self.valves.FAULTLINE_URL}/query",
                    json={"text": text, "user_id": user_id},
                )

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet query status: {response.status_code}")

            if response.status_code != 200:
                return body

            facts = response.json().get("facts", [])

            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet facts returned: {len(facts)}")

            if not facts:
                return body

            fact_lines = "\n".join(
                f"- {f.get('subject')} {f.get('rel_type')} {f.get('object')}"
                for f in facts
            )
            memory_block = f"\n\n---\n🧠 **Memory context from FaultLine:**\n{fact_lines}"

            messages = body.get("messages", [])
            for i in reversed(range(len(messages))):
                if messages[i].get("role") == "assistant":
                    messages[i]["content"] = messages[i]["content"] + memory_block
                    break

        except httpx.ConnectError:
            if self.valves.ENABLE_DEBUG:
                print("[FaultLine Filter] outlet: FaultLine unreachable")
        except httpx.TimeoutException:
            if self.valves.ENABLE_DEBUG:
                print("[FaultLine Filter] outlet: FaultLine timeout")
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet error: {e}")

        return body
