"""Ollama backend (local, free). Uses /api/chat with JSON-schema constrained decoding."""

from __future__ import annotations

from typing import Any

import httpx

from prospectpilot.config import Settings
from prospectpilot.llm.base import BackendResult, LLMError, LLMRequest


class OllamaBackend:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_s)

    async def generate(self, req: LLMRequest, model: str) -> BackendResult:
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.prompt},
            ],
            "options": {
                "temperature": req.temperature,
                "seed": self.settings.llm_seed,
                "num_ctx": self.settings.ollama_num_ctx,
                "num_predict": req.max_tokens,
            },
        }
        if req.json_schema is not None:
            payload["format"] = req.json_schema
        try:
            r = await self.client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama request failed: {exc}") from exc
        body = r.json()
        return BackendResult(
            text=body.get("message", {}).get("content", ""),
            input_tokens=int(body.get("prompt_eval_count") or 0),
            output_tokens=int(body.get("eval_count") or 0),
            stop_reason=body.get("done_reason"),
        )
