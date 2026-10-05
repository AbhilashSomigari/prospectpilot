"""Seed companies from explicit public website URLs listed in the ICP."""

from __future__ import annotations

from urllib.parse import urlsplit

from prospectpilot.models import CompanyCandidate
from prospectpilot.sources.http import PoliteFetcher
from prospectpilot.sources.pages import parse_page


def homepage_url(domain_or_url: str) -> str:
    if domain_or_url.startswith(("http://", "https://")):
        parts = urlsplit(domain_or_url)
        return f"{parts.scheme}://{parts.netloc}/"
    return f"https://{domain_or_url.strip('/')}/"


async def candidate_from_website(fetcher: PoliteFetcher, url: str) -> CompanyCandidate:
    home = homepage_url(url)
    res = await fetcher.fetch(home)
    page = parse_page(home, res.text)
    name = page.title.split("|")[0].split(" - ")[0].split("—")[0].strip() or urlsplit(home).netloc
    return CompanyCandidate(
        name=name,
        domain=urlsplit(home).netloc,
        source="website",
        source_url=home,
        description=page.description or page.text[:300],
    )
