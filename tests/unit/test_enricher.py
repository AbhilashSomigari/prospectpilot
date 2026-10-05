from __future__ import annotations

from datetime import date
from pathlib import Path

import respx

from prospectpilot.agents.enricher import collect_pages, extract_facts, supported_by, validate_facts
from prospectpilot.agents.heuristics import heuristic_facts
from prospectpilot.llm.client import LLM, track_usage
from prospectpilot.llm.embeddings import HashEmbedder
from prospectpilot.models import ExtractedFact, ExtractedFacts
from prospectpilot.sources.http import PoliteFetcher
from prospectpilot.sources.pages import Page, parse_page

from .helpers import FIX, make_settings, mount_site


@respx.mock
async def test_collect_pages_follows_sections_and_skips_disallowed(tmp_path: Path) -> None:
    mount_site(respx.mock, "acme-analytics.io")
    pages = await collect_pages(PoliteFetcher(make_settings(tmp_path)), "acme-analytics.io")
    urls = [p.url for p in pages]
    assert urls[0] == "https://acme-analytics.io/"
    assert "https://acme-analytics.io/about" in urls
    assert "https://acme-analytics.io/careers" in urls
    assert "https://acme-analytics.io/blog/launching-pipelines-2" in urls
    assert not any("private" in u for u in urls)
    assert len(pages) <= 6


async def test_mock_llm_extraction_produces_cited_facts(tmp_path: Path) -> None:
    f = PoliteFetcher(make_settings(tmp_path), offline=True, replay_dirs=[FIX / "sites"])
    pages = await collect_pages(f, "acme-analytics.io")
    llm = LLM(make_settings(tmp_path), provider="mock")
    with track_usage() as usage:
        facts = await extract_facts(llm, "Acme Analytics", pages)
    kinds = {fact.kind for fact in facts}
    assert {"product", "news", "open_role", "tech_stack", "team_size", "funding"} <= kinds
    fetched = {p.url for p in pages}
    assert all(fact.source_url in fetched for fact in facts)
    news = next(x for x in facts if x.kind == "news")
    assert news.published_at == date(2026, 8, 12)
    assert "Pipelines 2.0" in news.text
    assert len(usage.calls) == 1 and usage.purposes == ["extract"]


def test_validate_facts_drops_wrong_url_and_unsupported_text() -> None:
    page = Page(url="https://x.io/about", text="X has a team of 45 people in Austin.")
    extracted = ExtractedFacts(
        facts=[
            ExtractedFact(kind="team_size", text="X has a team of 45 people.", source_url="https://x.io/about/"),
            ExtractedFact(kind="funding", text="X raised a $50M Series C from Sequoia.", source_url="https://x.io/about"),
            ExtractedFact(kind="other", text="X has a team of 45 people.", source_url="https://x.io/elsewhere"),
        ]
    )  # fmt: skip
    kept = validate_facts(extracted, [page])
    assert [k.text for k in kept] == ["X has a team of 45 people."]
    assert kept[0].source_url == "https://x.io/about"


def test_supported_by_threshold() -> None:
    page = Page(url="u", text="Acme launched Pipelines 2.0 with column-level lineage.")
    assert supported_by("Acme launched Pipelines 2.0", page)
    assert not supported_by("Acme acquired a competitor in Brazil last year", page)


def test_heuristics_on_careers_page() -> None:
    html = (FIX / "sites/acme-analytics.io/careers.html").read_text()
    page = parse_page("https://acme-analytics.io/careers", html)
    facts = heuristic_facts("Acme Analytics", [page]).facts
    roles = [f.text for f in facts if f.kind == "open_role"]
    assert roles == [
        "Acme Analytics is hiring a Senior Platform Engineer.",
        "Acme Analytics is hiring a Founding Account Executive.",
    ]
    tech = next(f for f in facts if f.kind == "tech_stack")
    assert "Kubernetes" in tech.text and "Terraform" in tech.text


async def test_hash_embedder_is_normalized_and_lexical() -> None:
    emb = HashEmbedder(768)
    a, b, c = await emb.embed(
        ["ROI angle beats feature angle for CTOs", "for CTOs the ROI angle wins", "kubernetes"]
    )
    assert len(a) == 768
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6

    def dot(u: list[float], v: list[float]) -> float:
        return sum(x * y for x, y in zip(u, v, strict=True))

    assert dot(a, b) > dot(a, c)
