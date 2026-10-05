"""Hacker News "Ask HN: Who is hiring?" threads via the public Algolia HN API.

Each top-level comment is a job post, conventionally `Company | Role(s) | Location | ... | URL`.
"""

from __future__ import annotations

import html
import re
from typing import Any

from prospectpilot.models import CompanyCandidate, PersonCandidate
from prospectpilot.sources.http import PoliteFetcher

ALGOLIA = "https://hn.algolia.com/api/v1"
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9-]+\.)+(?:com|io|ai|dev|co|org|net|app|tech|so|xyz))\b", re.I
)
_SKIP_DOMAINS = (
    "news.ycombinator.com",
    "ycombinator.com",
    "github.com",
    "linkedin.com",
    "lever.co",
    "greenhouse.io",
    "ashbyhq.com",
    "workable.com",
    "notion.site",
    "google.com",
    "forms.gle",
    "wellfound.com",
    "x.com",
    "twitter.com",
    "germantechjobs.de",
    "workatastartup.com",
    "weworkremotely.com",
    "remoteok.com",
    "jobs.lever.co",
    "apply.workable.com",
    "bit.ly",
)


def _strip_html(text: str) -> str:
    text = re.sub(r"<p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _hrefs(raw: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'href="([^"]+)"', raw)]


def _pick_domain(urls: list[str], text: str) -> str | None:
    candidates = [*urls, *_URL_RE.findall(text), *_DOMAIN_RE.findall(text)]
    for c in candidates:
        host = re.sub(r"^https?://", "", c.lower()).split("/")[0].removeprefix("www.")
        if host and not any(host == d or host.endswith("." + d) for d in _SKIP_DOMAINS):
            if "." in host:
                return host
    return None


def parse_post(item: dict[str, Any], thread_url: str) -> CompanyCandidate | None:
    raw = item.get("text") or ""
    if not raw:
        return None
    text = _strip_html(raw)
    first_line = text.strip().split("\n", 1)[0]
    parts = [p.strip() for p in re.split(r"\s+[|—–]\s+|\s\|\s?", first_line) if p.strip()]
    if not parts:
        return None
    name = _URL_RE.sub("", parts[0])
    name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip(" -|,")
    if not name or len(name) > 60:
        return None
    domain = _pick_domain(_hrefs(raw), text)
    if domain is None:
        return None
    locations = [p for p in parts[1:] if re.search(r"remote|onsite|hybrid|,|\b[A-Z]{2}\b", p)]
    roles = [
        p.strip(" |-")
        for p in parts[1:]
        if re.search(r"engineer|developer|designer|manager|lead|scien", p, re.I)
    ]
    people = [
        PersonCandidate(email=e, source_url=f"https://news.ycombinator.com/item?id={item['id']}")
        for e in dict.fromkeys(_EMAIL_RE.findall(text))
        if domain in e.lower()
    ][:2]
    signals = ["hiring (HN Who is hiring)"] + [f"hiring: {r}" for r in roles[:3]]
    return CompanyCandidate(
        name=name,
        domain=domain,
        source="hn",
        source_url=f"https://news.ycombinator.com/item?id={item['id']}",
        description=text[:1200],
        signals=signals,
        people=people,
        locations=locations[:3],
    )


async def latest_hiring_thread(fetcher: PoliteFetcher) -> int:
    data = await fetcher.fetch_json(
        f"{ALGOLIA}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
    )
    for hit in data["hits"]:
        if str(hit.get("title", "")).startswith("Ask HN: Who is hiring?"):
            return int(hit["objectID"])
    raise LookupError("no 'Who is hiring?' thread found")


async def fetch_hiring_posts(
    fetcher: PoliteFetcher, thread_id: int | None = None, max_posts: int = 200
) -> list[CompanyCandidate]:
    tid = thread_id or await latest_hiring_thread(fetcher)
    thread = await fetcher.fetch_json(f"{ALGOLIA}/items/{tid}")
    thread_url = f"https://news.ycombinator.com/item?id={tid}"
    out: list[CompanyCandidate] = []
    for child in thread.get("children", [])[:max_posts]:
        cand = parse_post(child, thread_url)
        if cand is not None:
            out.append(cand)
    return out
