"""OpenTelemetry tracing: OTLP/HTTP -> collector -> Jaeger, plus an optional Langfuse exporter.

Every graph node, tool call and LLM call opens a span through `span()` / `traced()`.
LLM spans follow the GenAI semantic conventions (gen_ai.*) and add pp.cost_usd.
"""

from __future__ import annotations

import base64
import functools
import inspect
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from prospectpilot.config import get_settings

P = ParamSpec("P")
R = TypeVar("R")

_provider: TracerProvider | None = None


def setup_tracing(
    service_name: str | None = None, extra_processors: list[SpanProcessor] | None = None
) -> TracerProvider:
    """Idempotently install the global tracer provider."""
    global _provider
    if _provider is not None:
        for p in extra_processors or []:
            _provider.add_span_processor(p)
        return _provider
    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": service_name or settings.service_name,
            "deployment.environment": settings.env,
        }
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_enabled:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        if settings.langfuse_enabled and settings.langfuse_secret_key is not None:
            # Langfuse ingests OTLP natively; auth is HTTP basic with the project key pair.
            token = base64.b64encode(
                f"{settings.langfuse_public_key}:"
                f"{settings.langfuse_secret_key.get_secret_value()}".encode()
            ).decode()
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.langfuse_host.rstrip("/") + "/api/public/otel/v1/traces",
                        headers={"Authorization": f"Basic {token}"},
                    )
                )
            )
    for p in extra_processors or []:
        provider.add_span_processor(p)
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.force_flush(timeout_millis=5000)


def tracer() -> trace.Tracer:
    return trace.get_tracer("prospectpilot")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in attrs.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, str | bool | int | float) else str(v)
    return out


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    with tracer().start_as_current_span(name, attributes=_clean(attributes)) as s:
        try:
            yield s
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise


def set_attrs(s: Span, **attributes: Any) -> None:
    s.set_attributes(_clean(attributes))


def traced(
    name: str, **attributes: Any
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that wraps an async function in a span."""

    def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError("traced() only wraps async functions")

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(name, **attributes):
                return await fn(*args, **kwargs)

        return wrapper

    return deco


class TraceIdLogFilter:
    """Adds `trace_id` to log records so log lines can be joined with Jaeger traces."""

    def filter(self, record: object) -> bool:
        record.trace_id = current_trace_id() or "-"  # type: ignore[attr-defined]
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s trace=%(trace_id)s %(message)s"


def setup_logging(level: int = 20) -> None:
    import logging

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(TraceIdLogFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
