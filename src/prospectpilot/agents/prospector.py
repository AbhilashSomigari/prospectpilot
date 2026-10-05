"""Prospector: ICP -> deduplicated candidate companies from the allowed sources."""

from __future__ import annotations

import logging
from pathlib import Path

from prospectpilot.config import REPO_ROOT, get_settings
from prospectpilot.models import ICP, CompanyCandidate
from prospectpilot.obs.tracing import set_attrs, span
from prospectpilot.sources import github, hn
from prospectpilot.sources.csv_import import load_csv
from prospectpilot.sources.http import FetchError, PoliteFetcher
from prospectpilot.sources.website import candidate_from_website

log = logging.getLogger(__name__)


def _resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() or path.exists() else REPO_ROOT / path


def icp_score(icp: ICP, c: CompanyCandidate) -> float:
    """Soft relevance score. Explicit sources (csv/website) are curated by the user."""
    hay = " ".join([c.name, c.description, *c.signals]).lower()
    score = 0.0
    score += 2.0 * sum(1 for k in icp.industry_keywords if k.lower() in hay)
    score += 1.0 * sum(1 for k in icp.tech_stack if k.lower() in hay)
    score += 1.0 * sum(1 for k in icp.hiring_signals if k.lower() in hay)
    if icp.locations and c.locations:
        locs = " ".join(c.locations).lower()
        score += 1.0 * sum(1 for loc in icp.locations if loc.lower() in locs)
    return score


def in_team_size(icp: ICP, c: CompanyCandidate) -> bool:
    return c.team_size is None or icp.team_size.min <= c.team_size <= icp.team_size.max


def merge(candidates: list[CompanyCandidate]) -> list[CompanyCandidate]:
    """Dedupe by normalized domain, merging people and signals."""
    by_domain: dict[str, CompanyCandidate] = {}
    for c in candidates:
        cur = by_domain.get(c.domain)
        if cur is None:
            by_domain[c.domain] = c.model_copy(deep=True)
            continue
        cur.signals = list(dict.fromkeys([*cur.signals, *c.signals]))
        cur.locations = list(dict.fromkeys([*cur.locations, *c.locations]))
        seen = {(p.full_name or "", p.email or "") for p in cur.people}
        cur.people += [p for p in c.people if (p.full_name or "", p.email or "") not in seen]
        if len(c.description) > len(cur.description):
            cur.description = c.description
        cur.team_size = cur.team_size or c.team_size
    return list(by_domain.values())


async def gather(icp: ICP, fetcher: PoliteFetcher) -> list[CompanyCandidate]:
    settings = get_settings()
    token = settings.github_token.get_secret_value() if settings.github_token else None
    curated: list[CompanyCandidate] = []
    discovered: list[CompanyCandidate] = []
    src = icp.sources
    with span("agent.prospector.gather", **{"pp.icp": icp.name}) as s:
        if src.csv_path:
            curated += load_csv(_resolve_path(src.csv_path))
        for url in src.websites:
            try:
                curated.append(await candidate_from_website(fetcher, url))
            except FetchError as exc:
                log.warning("website source %s skipped: %s", url, exc)
        if src.hn.enabled:
            try:
                discovered += await hn.fetch_hiring_posts(
                    fetcher, src.hn.thread_id, src.hn.max_posts
                )
            except (FetchError, LookupError) as exc:
                log.warning("HN source failed: %s", exc)
        if src.github.enabled:
            logins = list(src.github.orgs)
            if src.github.search_query:
                try:
                    logins += await github.search_orgs(
                        fetcher, src.github.search_query, token, src.github.max_orgs
                    )
                except FetchError as exc:
                    log.warning("GitHub search failed: %s", exc)
            for login in dict.fromkeys(logins):
                try:
                    cand = await github.fetch_org(fetcher, login, token)
                except FetchError as exc:
                    log.warning("GitHub org %s skipped: %s", login, exc)
                    continue
                if cand is not None:
                    discovered.append(cand)
        has_filters = bool(icp.industry_keywords or icp.tech_stack or icp.hiring_signals)
        scored = [
            (icp_score(icp, c), c)
            for c in discovered
            if in_team_size(icp, c) and (not has_filters or icp_score(icp, c) > 0)
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        ranked = [c for c in curated if in_team_size(icp, c)] + [c for _, c in scored]
        result = merge(ranked)[: icp.max_companies]
        set_attrs(
            s,
            **{
                "pp.curated": len(curated),
                "pp.discovered": len(discovered),
                "pp.selected": len(result),
            },
        )
        return result
