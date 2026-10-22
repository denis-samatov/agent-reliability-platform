"""OpenAI-compatible chat client with native tool calling, budgets and GenAI spans.

Used with a local ``llama-server`` (``--jinja`` for tool-call templates); any
OpenAI-compatible endpoint works when configured explicitly. No external endpoint is
contacted unless ``ARP_LLM_BASE_URL`` points to one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

from arp.engine.executor import TransientError


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = os.environ.get("ARP_LLM_BASE_URL", "http://127.0.0.1:8081/v1")
    model: str = "qwen3-1.7b-q4_k_m"
    temperature: float = 0.0
    max_tokens: int = 384
    api_key: str | None = None

    def fingerprint(self) -> dict:
        return {"model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens}


class ChatLLM:
    def __init__(self, cfg: ModelConfig | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg or ModelConfig()
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        self.http = httpx.AsyncClient(base_url=self.cfg.base_url, timeout=120, headers=headers, transport=transport)

    async def chat(self, ctx, messages: list[dict], tools: list[dict] | None = None, tool_choice: str = "auto") -> dict:
        ctx.budget.check()
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        with ctx.tracer.llm_span(self.cfg.model) as span:
            try:
                r = await self.http.post("/chat/completions", json=payload)
            except httpx.TransportError as e:
                raise TransientError(f"llm transport: {e}") from e
            if r.status_code in (429, 500, 502, 503):
                raise TransientError(f"llm http {r.status_code}")
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage", {})
            pin, pout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            span.set_attribute("gen_ai.usage.input_tokens", pin)
            span.set_attribute("gen_ai.usage.output_tokens", pout)
            ctx.budget.add_llm(pin, pout)
            ctx.store.append(
                ctx.run_id,
                "llm_call",
                ctx.current_step,
                {"model": self.cfg.model, "input_tokens": pin, "output_tokens": pout},
            )
            ctx.budget.check()
            msg = data["choices"][0]["message"]
            calls = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"__invalid_json__": fn.get("arguments")}
                calls.append({"id": tc.get("id", ""), "name": fn.get("name"), "arguments": args})
            return {"content": msg.get("content") or "", "tool_calls": calls}
