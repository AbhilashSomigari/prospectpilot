from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from prospectpilot.config import Settings
from prospectpilot.llm.base import LLMRequest
from prospectpilot.llm.client import LLM
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import setup_tracing
from prospectpilot.sources.http import PoliteFetcher

from .helpers import FIX, make_settings

EXPORTER = InMemorySpanExporter()
setup_tracing(extra_processors=[SimpleSpanProcessor(EXPORTER)])  # once per process


@pytest.fixture(autouse=True)
def spans() -> InMemorySpanExporter:
    EXPORTER.clear()
    return EXPORTER


async def test_llm_span_has_tokens_cost_and_latency(
    tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    settings = make_settings(tmp_path)
    settings.pricing["mock"] = settings.price_for("claude-haiku-4-5")  # give the mock a price
    llm = LLM(settings, provider="mock")
    req = LLMRequest(
        purpose="critic",
        system="s",
        prompt="p" * 400,
        context={"sequence": {"emails": []}, "offer": {}},
    )
    before = metrics.LLM_COST.labels("mock", "mock", "critic")._value.get()
    resp = await llm.complete(req)
    (span,) = [s for s in spans.get_finished_spans() if s.name == "llm.critic"]
    a = span.attributes or {}
    assert a["gen_ai.request.model"] == "mock" and a["gen_ai.system"] == "mock"
    assert a["gen_ai.usage.input_tokens"] == resp.input_tokens > 0
    assert a["gen_ai.usage.output_tokens"] == resp.output_tokens > 0
    assert a["pp.cost_usd"] == pytest.approx(resp.cost_usd) and resp.cost_usd > 0
    assert a["pp.latency_ms"] >= 0
    after = metrics.LLM_COST.labels("mock", "mock", "critic")._value.get()
    assert after - before == pytest.approx(resp.cost_usd)


async def test_tool_span_for_fetch(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    f = PoliteFetcher(make_settings(tmp_path), offline=True, replay_dirs=[FIX / "sites"])
    await f.fetch("https://acme-analytics.io/about")
    (span,) = [s for s in spans.get_finished_spans() if s.name == "tool.http_fetch"]
    assert (span.attributes or {})["pp.cache_hit"] is True


def test_api_requests_are_traced(spans: InMemorySpanExporter) -> None:
    from prospectpilot.api.app import app

    with TestClient(app) as client:
        r = client.get("/healthz")
    assert len(r.headers["X-Trace-Id"]) == 32
    assert any(s.name == "http GET" for s in spans.get_finished_spans())


def test_dashboard_is_valid_and_covers_required_panels() -> None:
    import json

    dash = json.loads(
        (Path(__file__).parents[2] / "docker/grafana/dashboards/prospectpilot.json").read_text()
    )
    titles = {p["title"] for p in dash["panels"]}
    for required in (
        "Quality: grounded-and-passing rate",
        "Cost per lead (USD)",
        "Latency per lead p50 (ms)",
        "Graph node duration p95",
        "Improvement changelog",
    ):
        assert required in titles
    assert len({p["id"] for p in dash["panels"]}) == len(dash["panels"])


def test_settings_defaults_keep_otel_off_in_tests() -> None:
    assert Settings().otel_enabled is False
