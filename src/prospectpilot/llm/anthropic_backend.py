"""Anthropic Messages API backend (official `anthropic` SDK, 1.x)."""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from prospectpilot.config import Settings
from prospectpilot.llm.base import BackendResult, LLMError, LLMRefusal, LLMRequest

log = logging.getLogger(__name__)

# Keywords structured outputs may not accept; Pydantic re-validates them after the call anyway.
_UNSUPPORTED_SCHEMA_KEYS = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "default",
    "title",
}


def to_strict_schema(schema: Any) -> Any:
    """Make a Pydantic JSON schema acceptable to `output_config.format`.

    Adds additionalProperties=false + full `required` on every object and drops validation
    keywords (the response is re-validated by Pydantic, so nothing is lost).
    """
    if isinstance(schema, list):
        return [to_strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: to_strict_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"].keys())
    return out


def _supports_sampling_params(model: str) -> bool:
    # Sonnet 5.5 / Opus 5.x reject non-default temperature; Haiku 4.5 accepts it.
    return model.startswith("claude-haiku")


def _supports_effort(model: str) -> bool:
    return not model.startswith("claude-haiku")


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        self.client = anthropic.AsyncAnthropic(api_key=key, timeout=settings.llm_timeout_s)
        self.settings = settings
        self._structured = settings.anthropic_structured_outputs

    def _params(self, req: LLMRequest, model: str, structured: bool) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        output_config: dict[str, Any] = {}
        if _supports_effort(model):
            output_config["effort"] = self.settings.anthropic_effort
        if structured and req.json_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": to_strict_schema(req.json_schema),
            }
        if output_config:
            params["output_config"] = output_config
        if _supports_sampling_params(model):
            params["temperature"] = req.temperature
        if self.settings.anthropic_refusal_fallback and model.startswith("claude-sonnet-5-5"):
            # Server-side refusal fallback ("default" routing by refusal category).
            params["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            params["extra_body"] = {"fallbacks": "default"}
        return params

    async def generate(self, req: LLMRequest, model: str) -> BackendResult:
        try:
            msg = await self.client.messages.create(**self._params(req, model, self._structured))
        except anthropic.BadRequestError as exc:
            if not (self._structured and req.json_schema is not None):
                raise LLMError(f"anthropic 400: {exc}") from exc
            # Schema not accepted: fall back to prompt-only JSON for the rest of the process.
            log.warning("structured outputs rejected (%s); falling back to prompt-only JSON", exc)
            self._structured = False
            msg = await self.client.messages.create(**self._params(req, model, False))
        if msg.stop_reason == "refusal":
            raise LLMRefusal(f"{model} refused the {req.purpose} request")
        text = "".join(block.text for block in msg.content if block.type == "text")
        return BackendResult(
            text=text,
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            stop_reason=msg.stop_reason,
        )
