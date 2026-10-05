"""Deterministic mock backend: no network, no keys.

Dispatches on `LLMRequest.purpose` and builds a valid reply from `LLMRequest.context` (the
structured inputs behind the prompt). This makes the demo, CI and the e2e tests reproducible,
and exercises every downstream parser/validator with realistic payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from prospectpilot.llm.base import BackendResult, LLMError, LLMRequest

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def _extract(ctx: dict[str, Any]) -> dict[str, Any]:
    from prospectpilot.agents.heuristics import heuristic_facts
    from prospectpilot.sources.pages import Page

    pages = [Page.model_validate(p) for p in ctx.get("pages", [])]
    return heuristic_facts(str(ctx.get("company", "The company")), pages).model_dump(mode="json")


def _lazy(module: str, fn: str) -> Handler:
    def call(ctx: dict[str, Any]) -> dict[str, Any]:
        import importlib

        result: dict[str, Any] = getattr(importlib.import_module(module), fn)(ctx)
        return result

    return call


HANDLERS: dict[str, Handler] = {
    "extract": _extract,
    "writer": _lazy("prospectpilot.llm.mock_handlers", "writer"),
    "critic": _lazy("prospectpilot.llm.mock_handlers", "critic"),
    "reply_sim": _lazy("prospectpilot.llm.mock_handlers", "reply_sim"),
    "failure_analysis": _lazy("prospectpilot.llm.mock_handlers", "failure_analysis"),
    "optimizer": _lazy("prospectpilot.llm.mock_handlers", "optimizer"),
}


class MockBackend:
    name = "mock"

    async def generate(self, req: LLMRequest, model: str) -> BackendResult:
        handler = HANDLERS.get(req.purpose)
        if handler is None:
            raise LLMError(f"mock backend has no handler for purpose {req.purpose!r}")
        text = json.dumps(handler(req.context))
        return BackendResult(
            text=text,
            input_tokens=(len(req.system) + len(req.prompt)) // 4,
            output_tokens=len(text) // 4,
            stop_reason="end_turn",
        )
