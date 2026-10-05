"""GitHub organizations via the public REST API (token optional, raises rate limits)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from prospectpilot.models import CompanyCandidate, PersonCandidate
from prospectpilot.sources.http import FetchError, PoliteFetcher

API = "https://api.github.com"


def _headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def org_to_candidate(org: dict[str, Any], people: list[PersonCandidate]) -> CompanyCandidate | None:
    blog = (org.get("blog") or "").strip()
    if not blog:
        return None
    signals = [f"{org.get('public_repos', 0)} public GitHub repos"]
    return CompanyCandidate(
        name=org.get("name") or org["login"],
        domain=blog,
        source="github",
        source_url=org.get("html_url") or f"https://github.com/{org['login']}",
        description=org.get("description") or "",
        signals=signals,
        people=people,
        locations=[org["location"]] if org.get("location") else [],
    )


async def fetch_org(
    fetcher: PoliteFetcher, login: str, token: str | None = None, max_members: int = 3
) -> CompanyCandidate | None:
    h = _headers(token)
    org = await fetcher.fetch_json(f"{API}/orgs/{quote(login)}", headers=h)
    people: list[PersonCandidate] = []
    try:
        members = await fetcher.fetch_json(
            f"{API}/orgs/{quote(login)}/public_members?per_page={max_members}", headers=h
        )
    except FetchError:
        members = []
    for m in members[:max_members]:
        try:
            user = await fetcher.fetch_json(f"{API}/users/{quote(m['login'])}", headers=h)
        except FetchError:
            continue
        if user.get("name"):
            people.append(
                PersonCandidate(
                    full_name=user["name"],
                    title=None,
                    # only a *published* profile email is used; nothing is guessed here
                    email=user.get("email"),
                    source_url=user.get("html_url"),
                )
            )
    return org_to_candidate(org, people)


async def search_orgs(
    fetcher: PoliteFetcher, query: str, token: str | None = None, limit: int = 20
) -> list[str]:
    q = quote(f"{query} type:org")
    data = await fetcher.fetch_json(
        f"{API}/search/users?q={q}&per_page={min(limit, 50)}", headers=_headers(token)
    )
    return [item["login"] for item in data.get("items", [])][:limit]
