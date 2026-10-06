"""OpenAI-compatible /chat/completions backend (vLLM, LM Studio, gateways, ...)."""

from __future__ import annotations

from typing import Any

import httpx

from prospectpilot.config import Settings
from prospectpilot.llm.base import BackendResult, LLMError, LLMRequest


class OpenAICompatBackend:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.openai_base_url.rstrip("/")
        headers = {}
        if settings.openai_api_key is not None:
            headers["Authorization"] = f"Bearer {settings.openai_api_key.get_secret_value()}"
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_s, headers=headers)

    async def generate(self, req: LLMRequest, model: str) -> BackendResult:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "seed": req.seed if req.seed is not None else self.settings.llm_seed,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt},
            ],
        }
        if req.json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = await self.client.post(f"{self.base_url}/chat/completions", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"openai-compatible request failed: {exc}") from exc
        body = r.json()
        choice = body["choices"][0]
        usage = body.get("usage") or {}
        return BackendResult(
            text=choice["message"].get("content") or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            stop_reason=choice.get("finish_reason"),
        )
