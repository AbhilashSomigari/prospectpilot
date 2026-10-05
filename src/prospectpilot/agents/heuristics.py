"""Deterministic, rule-based fact extraction.

Used (1) by the mock LLM backend so the whole pipeline runs with no model, and (2) as the
fallback when a real model fails to return valid facts. Every fact is copied verbatim-ish from
one page and carries that page's URL, so it is grounded by construction.
"""

from __future__ import annotations

import re
from datetime import date

from prospectpilot.models import ExtractedFact, ExtractedFacts
from prospectpilot.sources.pages import Page

TECH_TERMS = (
    "Python", "Go", "Rust", "TypeScript", "React", "Node.js", "Kubernetes", "Terraform", "AWS",
    "GCP", "Azure", "Postgres", "PostgreSQL", "Snowflake", "dbt", "Kafka", "Spark", "Airflow",
    "Django", "FastAPI", "Rails", "Elixir", "Java", "Kotlin", "Swift", "GraphQL", "Redis",
    "ClickHouse", "PyTorch", "LangChain", "LangGraph", "OpenTelemetry", "Docker", "Next.js",
)  # fmt: skip
_ROLE_RE = re.compile(
    r"^(?:Senior |Staff |Lead |Principal |Junior )?[A-Z][\w/&+ -]{2,50}"
    r"(Engineer|Developer|Designer|Manager|Scientist|Representative|Executive|Lead|Analyst|"
    r"Architect|Recruiter|Marketer)\b.*$"
)
_ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_LONG_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s+(20\d\d)\b"
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
    )
}
_TEAM_RE = re.compile(
    r"\b(\d[\d,]*)\+?\s+(people|employees|engineers|team members|teammates)\b", re.I
)
_FUNDING_RE = re.compile(r"[^.]*\b(raised|Series [A-E]|seed round|funding)\b[^.]*\.", re.I)


def _parse_date(text: str) -> date | None:
    if m := _ISO_DATE.search(text):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if m := _LONG_DATE.search(text):
        return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    return None


def _first_sentence(text: str, max_len: int = 240) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.{20,}?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s[:max_len].rstrip()


def _kind_of(page: Page) -> str:
    u = page.url.lower()
    if any(k in u for k in ("career", "job", "join", "hiring")):
        return "careers"
    if any(k in u for k in ("blog", "news", "press", "changelog", "update")):
        return "blog"
    if any(k in u for k in ("about", "company", "team", "story")):
        return "about"
    return "home"


def heuristic_facts(company: str, pages: list[Page], max_facts: int = 10) -> ExtractedFacts:
    facts: list[ExtractedFact] = []
    seen: set[str] = set()

    def add(kind: str, text: str, url: str, published: date | None = None) -> None:
        text = " ".join(text.split())
        key = text.lower()
        if len(text) < 8 or key in seen:
            return
        seen.add(key)
        facts.append(
            ExtractedFact(kind=kind, text=text[:400], source_url=url, published_at=published)
        )

    tech_found: dict[str, str] = {}
    for page in pages:
        kind = _kind_of(page)
        if kind == "home":
            desc = page.description or _first_sentence(page.text)
            if desc:
                add("product", f"{company}: {desc}", page.url)
        elif kind == "about":
            if m := _TEAM_RE.search(page.text):
                sentence = _sentence_containing(page.text, m.group(0))
                add("team_size", sentence or f"{company} has {m.group(0)}.", page.url)
            if m2 := _FUNDING_RE.search(page.text):
                add("funding", m2.group(0).strip(), page.url)
        elif kind == "blog":
            published = _parse_date(" ".join(page.times) + " " + page.text[:400])
            title = page.headings[0] if page.headings else page.title
            if title and published:
                summary = _first_sentence(page.text.replace(title, "", 1))
                add("news", f"{company} published '{title}' on {published.isoformat()}: "
                    f"{summary}", page.url, published)  # fmt: skip
        elif kind == "careers":
            roles = [h for h in page.headings if _ROLE_RE.match(h)]
            for role in roles[:3]:
                add("open_role", f"{company} is hiring a {role}.", page.url)
        for term in TECH_TERMS:
            if term not in tech_found and re.search(
                rf"(?<![\w.]){re.escape(term)}(?![\w])", page.text
            ):
                tech_found[term] = page.url
    if tech_found:
        by_url: dict[str, list[str]] = {}
        for term, url in tech_found.items():
            by_url.setdefault(url, []).append(term)
        url, terms = max(by_url.items(), key=lambda kv: len(kv[1]))
        add("tech_stack", f"{company} mentions using {', '.join(terms[:6])}.", url)
    return ExtractedFacts(facts=facts[:max_facts])


def _sentence_containing(text: str, needle: str) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        if needle in sentence:
            return sentence[:300]
    return None
