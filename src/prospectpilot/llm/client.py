"""LLM router: picks backend+model per purpose, traces, meters, prices and records every call."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from prospectpilot.config import Settings, get_settings
from prospectpilot.llm.base import (
    LARGE_PURPOSES,
    Backend,
    LLMError,
    LLMRequest,
    LLMResponse,
)
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import current_trace_id, set_attrs, span

T = TypeVar("T", bound=BaseModel)


@dataclass
class UsageTracker:
    """Accumulates LLM usage for a scope (a lead, an eval task, a run)."""

    calls: list[LLMResponse] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


_trackers: contextvars.ContextVar[tuple[UsageTracker, ...]] = contextvars.ContextVar(
    "pp_usage_trackers", default=()
)


@contextmanager
def track_usage() -> Iterator[UsageTracker]:
    """Every LLM call made inside this block (incl. nested tasks) is added to the tracker."""
    tracker = UsageTracker()
    token = _trackers.set((*_trackers.get(), tracker))
    try:
        yield tracker
    finally:
        _trackers.reset(token)


def _record(resp: LLMResponse, purpose: str) -> None:
    for t in _trackers.get():
        t.calls.append(resp)
        t.purposes.append(purpose)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating code fences and leading/trailing prose."""
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON object found in model output")
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start : end + 1])


class LLM:
    def __init__(
        self,
        settings: Settings | None = None,
        backends: dict[str, Backend] | None = None,
        provider: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.provider = provider or self.settings.llm_provider
        self._backends: dict[str, Backend] = dict(backends or {})
        self._sem = asyncio.Semaphore(self.settings.llm_max_concurrency)

    # ---------------------------------------------------------------- routing
    def model_for(self, purpose: str) -> str:
        s = self.settings
        large = purpose in LARGE_PURPOSES
        if self.provider == "anthropic":
            return s.anthropic_large_model if large else s.anthropic_small_model
        if self.provider == "openai":
            return (s.openai_large_model if large else s.openai_small_model) or "gpt-compatible"
        if self.provider == "ollama":
            return s.ollama_large_model if large else s.ollama_small_model
        return "mock"

    def models(self) -> dict[str, str]:
        return {"large": self.model_for("writer"), "small": self.model_for("critic")}

    def backend(self) -> Backend:
        if self.provider not in self._backends:
            self._backends[self.provider] = _make_backend(self.provider, self.settings)
        return self._backends[self.provider]

    # ------------------------------------------------------------------ calls
    async def complete(self, req: LLMRequest) -> LLMResponse:
        model = self.model_for(req.purpose)
        backend = self.backend()
        with span(
            f"llm.{req.purpose}",
            **{
                "gen_ai.system": backend.name,
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": model,
                "gen_ai.request.max_tokens": req.max_tokens,
                "pp.purpose": req.purpose,
                "pp.json_mode": req.json_schema is not None,
            },
        ) as s:
            started = time.perf_counter()
            status = "ok"
            try:
                async with self._sem:
                    result = await backend.generate(req, model)
            except Exception:
                status = "error"
                raise
            finally:
                elapsed = time.perf_counter() - started
                metrics.LLM_CALLS.labels(backend.name, model, req.purpose, status).inc()
                metrics.LLM_LATENCY.labels(backend.name, model, req.purpose).observe(elapsed)
            price = self.settings.price_for(model)
            cost = (result.input_tokens * price.input + result.output_tokens * price.output) / 1e6
            resp = LLMResponse(
                text=result.text,
                provider=backend.name,
                model=model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=cost,
                latency_ms=elapsed * 1000,
                stop_reason=result.stop_reason,
            )
            set_attrs(
                s,
                **{
                    "gen_ai.response.model": model,
                    "gen_ai.usage.input_tokens": resp.input_tokens,
                    "gen_ai.usage.output_tokens": resp.output_tokens,
                    "gen_ai.response.finish_reasons": resp.stop_reason,
                    "pp.cost_usd": round(cost, 8),
                    "pp.latency_ms": round(resp.latency_ms, 1),
                },
            )
            metrics.LLM_TOKENS.labels(backend.name, model, "input").inc(resp.input_tokens)
            metrics.LLM_TOKENS.labels(backend.name, model, "output").inc(resp.output_tokens)
            metrics.LLM_COST.labels(backend.name, model, req.purpose).inc(cost)
            _record(resp, req.purpose)
            return resp

    async def complete_json(self, req: LLMRequest, schema: type[T], repairs: int = 1) -> T:
        """Call the model and validate its JSON against `schema`, with repair retries."""
        if req.json_schema is None:
            req = req.model_copy(update={"json_schema": schema.model_json_schema()})
        last_err: Exception | None = None
        prompt = req.prompt
        for _attempt in range(repairs + 1):
            resp = await self.complete(req.model_copy(update={"prompt": prompt}))
            try:
                return schema.model_validate(extract_json(resp.text))
            except (ValueError, ValidationError) as exc:
                last_err = exc
                prompt = (
                    f"{req.prompt}\n\nYour previous reply was invalid: {str(exc)[:600]}\n"
                    "Reply again with ONLY a single valid JSON object matching the schema."
                )
        raise LLMError(f"{req.purpose}: invalid JSON after {repairs + 1} attempts: {last_err}")

    def trace_id(self) -> str | None:
        return current_trace_id()


def _make_backend(provider: str, settings: Settings) -> Backend:
    if provider == "anthropic":
        from prospectpilot.llm.anthropic_backend import AnthropicBackend

        return AnthropicBackend(settings)
    if provider == "openai":
        from prospectpilot.llm.openai_backend import OpenAICompatBackend

        return OpenAICompatBackend(settings)
    if provider == "ollama":
        from prospectpilot.llm.ollama_backend import OllamaBackend

        return OllamaBackend(settings)
    if provider == "mock":
        from prospectpilot.llm.mock_backend import MockBackend

        return MockBackend()
    raise LLMError(f"unknown LLM provider {provider!r}")


_default: LLM | None = None


def get_llm() -> LLM:
    global _default
    if _default is None:
        _default = LLM()
    return _default
