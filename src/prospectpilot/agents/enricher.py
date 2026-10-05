"""Enricher: fetch homepage/about/blog/careers, LLM-extract cited facts, validate grounding."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from prospectpilot.agents.heuristics import heuristic_facts
from prospectpilot.llm.base import LLMError, LLMRequest
from prospectpilot.llm.client import LLM
from prospectpilot.models import ExtractedFact, ExtractedFacts
from prospectpilot.obs.tracing import set_attrs, span
from prospectpilot.prompts import load as load_prompt
from prospectpilot.sources.http import FetchError, PoliteFetcher
from prospectpilot.sources.pages import Page, blog_post_links, discover_sections, parse_page

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "by",
        "at",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "has",
        "have",
        "its",
        "it",
        "this",
        "that",
        "their",
        "our",
        "we",
        "they",
        "as",
        "into",
        "new",
        "now",
        "more",
        "than",
        "also",
        "about",
    ]
)


def _norm_url(u: str) -> str:
    return u.rstrip("/").lower().replace("://www.", "://")


async def collect_pages(fetcher: PoliteFetcher, domain: str, max_pages: int = 6) -> list[Page]:
    """Homepage + about + careers + blog index + latest blog posts (same site only)."""
    home_url = f"https://{domain}/"
    with span("tool.collect_pages", **{"pp.domain": domain}) as s:
        try:
            home = parse_page(home_url, (await fetcher.fetch(home_url)).text)
        except FetchError as exc:
            log.info("homepage unavailable for %s: %s", domain, exc)
            set_attrs(s, **{"pp.pages": 0})
            return []
        pages = [home]
        sections = discover_sections(home)
        queue = sections["about"] + sections["careers"] + sections["blog"]
        blog_indexes = set(sections["blog"])
        seen = {_norm_url(home_url)}
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if _norm_url(url) in seen:
                continue
            seen.add(_norm_url(url))
            try:
                page = parse_page(url, (await fetcher.fetch(url)).text)
            except FetchError as exc:
                log.info("skip %s: %s", url, exc)
                continue
            pages.append(page)
            if url in blog_indexes:
                queue += blog_post_links(page, limit=2)
        set_attrs(s, **{"pp.pages": len(pages)})
        return pages


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOP}


def supported_by(fact_text: str, page: Page, threshold: float = 0.5) -> bool:
    """Lexical support: enough of the fact's content words appear on the cited page."""
    words = _content_words(fact_text)
    if not words:
        return False
    page_words = _content_words(" ".join([page.title, page.description, page.text, *page.headings]))
    return len(words & page_words) / len(words) >= threshold


def validate_facts(extracted: ExtractedFacts, pages: list[Page]) -> list[ExtractedFact]:
    """Keep only facts whose source_url is a fetched page that actually supports the text."""
    by_url = {_norm_url(p.url): p for p in pages}
    out: list[ExtractedFact] = []
    seen: set[str] = set()
    for f in extracted.facts:
        page = by_url.get(_norm_url(f.source_url))
        if page is None or not supported_by(f.text, page):
            continue
        key = f.text.lower()
        if key not in seen:
            seen.add(key)
            out.append(f.model_copy(update={"source_url": page.url}))
    return out


def _render_pages(pages: list[Page], per_page_chars: int = 2500) -> str:
    blocks = []
    for p in pages:
        dates = f"\nDATES: {', '.join(p.times[:5])}" if p.times else ""
        blocks.append(
            f"URL: {p.url}\nTITLE: {p.title}\nDESCRIPTION: {p.description}{dates}\n"
            f"HEADINGS: {' | '.join(p.headings[:15])}\nTEXT:\n{p.text[:per_page_chars]}"
        )
    return "\n\n-----\n\n".join(blocks)


async def extract_facts(llm: LLM, company: str, pages: list[Page]) -> list[ExtractedFact]:
    if not pages:
        return []
    with span("agent.enricher.extract", **{"pp.company": company, "pp.pages": len(pages)}) as s:
        req = LLMRequest(
            purpose="extract",
            system=load_prompt("extract"),
            prompt=f"Company: {company}\n\n{_render_pages(pages)}",
            max_tokens=1500,
            temperature=0.0,
            context={"company": company, "pages": [p.model_dump() for p in pages]},
        )
        try:
            extracted = await llm.complete_json(req, ExtractedFacts)
            facts = validate_facts(extracted, pages)
            dropped = len(extracted.facts) - len(facts)
        except LLMError as exc:
            log.warning(
                "LLM extraction failed for %s (%s); using rule-based extraction", company, exc
            )
            extracted, dropped = heuristic_facts(company, pages), 0
            facts = validate_facts(extracted, pages)
        if len(facts) < 2:
            # top up with rule-based facts so the writer has something grounded to cite
            extra = validate_facts(heuristic_facts(company, pages), pages)
            known = {f.text.lower() for f in facts}
            facts += [f for f in extra if f.text.lower() not in known]
        set_attrs(s, **{"pp.facts": len(facts), "pp.facts_dropped_ungrounded": dropped})
        return facts[:10]


def fetched_at_now() -> datetime:
    return datetime.now(UTC)
