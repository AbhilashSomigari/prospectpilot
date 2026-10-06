"""Provider-neutral LLM types."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Purpose = Literal["extract", "writer", "critic", "optimizer", "failure_analysis", "reply_sim"]
LARGE_PURPOSES: frozenset[str] = frozenset({"writer", "optimizer"})


class LLMRequest(BaseModel):
    purpose: Purpose
    system: str
    prompt: str
    max_tokens: int = 2048
    temperature: float = 0.2
    json_schema: dict[str, Any] | None = None
    seed: int | None = None  # per-request seed (ollama/openai); evals vary it per trial
    # Structured inputs behind the prompt. Used by the mock backend and for trace attributes;
    # real backends only ever see `system` + `prompt`.
    context: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    stop_reason: str | None = None


class BackendResult(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class Backend(Protocol):
    name: str

    async def generate(self, req: LLMRequest, model: str) -> BackendResult: ...
