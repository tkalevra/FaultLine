"""
title: FaultLine WGM Filter
author: tkalevra
version: 1.1.0
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
    
    Intercepts user and assistant messages to:
    - inlet: Extract and commit facts from user messages
    - outlet: Query stored facts and augment assistant responses with memory context
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
        """Extract the most recent message from a given role."""
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
        """
        Fire ingest request to FaultLine WGM pipeline.
        Expects edges to be a list of {"subject": str, "object": str, "rel_type": str}.
        If edges is None or empty, FaultLine will infer them from text.
        """
        try:
            payload = {
                "text": text,
                "source": source,
                "user_id": user_id,
                "known_types": ["Person", "Organization", "Location", "Event", "Concept"],
            }
            # Only include edges if provided
            if edges:
                payload["edges"] = edges

            async with httpx.AsyncClient(
                timeout=self.valves.FAULTLINE_TIMEOUT
            ) as client:
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
        Inlet: Extract and commit facts from user messages asynchronously.
        
        This is called before the model processes the user's request.
        We fire an ingest request but do NOT block the message flow.
        """
        if not self.valves.ENABLED or not self.valves.INGEST_ENABLED:
            return body

        try:
            text = self._last_message(body.get("messages", []), "user")
            if text:
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] inlet firing ingest: {text[:80]}")
                user_id = __user__.get("id", "anonymous") if __user__ else "anonymous"
                # Fire and forget — don't await
                asyncio.create_task(
                    self._fire_ingest(
                        text,
                        self.valves.DEFAULT_SOURCE,
                        user_id,
                        edges=None,  # Let FaultLine infer edges from text
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
        Outlet: Query stored facts and augment assistant response with memory context.
        
        This is called after the model generates a response.
        We query FaultLine for related facts and append them as a memory block.
        """
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
                        "user_id": user_id,
                    },
                )

            if response.status_code != 200:
                if self.valves.ENABLE_DEBUG:
                    print(f"[FaultLine Filter] Query returned status {response.status_code}")
                return body

            data = response.json()
            facts = data.get("facts", [])

            if not facts:
                return body

            # Format facts as readable memory block
            fact_lines = "\n".join(
                f"- {f.get('subject')} {f.get('rel_type')} {f.get('object')}"
                for f in facts
            )

            memory_block = f"\n\n---\n🧠 **Memory context from FaultLine:**\n{fact_lines}"

            # Append to assistant's last message
            messages = body.get("messages", [])
            for i in reversed(range(len(messages))):
                if messages[i].get("role") == "assistant":
                    messages[i]["content"] = messages[i]["content"] + memory_block
                    break

        except httpx.ConnectError:
            if self.valves.ENABLE_DEBUG:
                print("[FaultLine Filter] Query: FaultLine unreachable")
        except httpx.TimeoutException:
            if self.valves.ENABLE_DEBUG:
                print("[FaultLine Filter] Query: FaultLine timeout")
        except Exception as e:
            if self.valves.ENABLE_DEBUG:
                print(f"[FaultLine Filter] outlet error: {e}")

        return body
