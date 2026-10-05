from __future__ import annotations

from pathlib import Path

import httpx
import respx

from prospectpilot.agents.prospector import gather, icp_score, merge
from prospectpilot.models import ICP, CompanyCandidate, Offer, PersonCandidate
from prospectpilot.sources import github, hn
from prospectpilot.sources.csv_import import load_csv
from prospectpilot.sources.http import PoliteFetcher

from .helpers import FIX, json_fixture, make_settings, mount_site


def _icp(**kw: object) -> ICP:
    return ICP.model_validate({"name": "t", "offer": Offer(product="X").model_dump(), **kw})


@respx.mock
async def test_hn_latest_thread_and_posts(tmp_path: Path) -> None:
    respx.get(url__startswith="https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, text=json_fixture("hn_search.json"))
    )
    respx.get("https://hn.algolia.com/api/v1/items/900001").mock(
        return_value=httpx.Response(200, text=json_fixture("hn_item.json"))
    )
    respx.get("https://hn.algolia.com/robots.txt").mock(return_value=httpx.Response(404))
    f = PoliteFetcher(make_settings(tmp_path))
    posts = await hn.fetch_hiring_posts(f)
    by_domain = {p.domain: p for p in posts}
    assert set(by_domain) == {"luminadata.io", "northwind-robotics.com", "quillstack.dev"}
    lumina = by_domain["luminadata.io"]
    assert lumina.name == "Lumina Data"
    assert lumina.people[0].email == "priya@luminadata.io"  # published in the post
    assert any("Senior Data Engineer" in s for s in lumina.signals)
    assert by_domain["northwind-robotics.com"].name == "Northwind Robotics"


@respx.mock
async def test_github_org_with_public_members(tmp_path: Path) -> None:
    respx.get("https://api.github.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://api.github.com/orgs/acme-analytics").mock(
        return_value=httpx.Response(200, text=json_fixture("github_org.json"))
    )
    respx.get(url__startswith="https://api.github.com/orgs/acme-analytics/public_members").mock(
        return_value=httpx.Response(200, text=json_fixture("github_members.json"))
    )
    for login in ("jdoe", "nobody"):
        respx.get(f"https://api.github.com/users/{login}").mock(
            return_value=httpx.Response(200, text=json_fixture(f"github_user_{login}.json"))
        )
    f = PoliteFetcher(make_settings(tmp_path))
    cand = await github.fetch_org(f, "acme-analytics", token="t0k")
    assert cand is not None
    assert cand.domain == "acme-analytics.io"
    assert [p.full_name for p in cand.people] == ["Jordan Doe"]  # nameless member skipped
    req = respx.calls.last.request
    assert req.headers["authorization"] == "Bearer t0k"


def test_csv_import_groups_people_by_domain() -> None:
    cands = load_csv(FIX / "leads_salesnav.csv")
    by_domain = {c.domain: c for c in cands}
    assert set(by_domain) == {"acme-analytics.io", "bigcorp.example"}
    acme = by_domain["acme-analytics.io"]
    assert [p.full_name for p in acme.people] == ["Maya Chen", "Sam Okafor"]
    assert acme.team_size == 30
    assert by_domain["bigcorp.example"].team_size == 10001


def test_merge_dedupes_by_domain() -> None:
    a = CompanyCandidate(
        name="Acme",
        domain="https://www.acme.io/x",
        source="hn",
        source_url="u1",
        signals=["hiring"],
        people=[PersonCandidate(full_name="A")],
    )
    b = CompanyCandidate(
        name="Acme Inc",
        domain="acme.io",
        source="github",
        source_url="u2",
        description="longer description",
        signals=["oss"],
        people=[PersonCandidate(full_name="A"), PersonCandidate(full_name="B")],
    )
    merged = merge([a, b])
    assert len(merged) == 1
    m = merged[0]
    assert m.domain == "acme.io" and m.signals == ["hiring", "oss"]
    assert [p.full_name for p in m.people] == ["A", "B"]
    assert m.description == "longer description"


def test_icp_scoring() -> None:
    icp = _icp(industry_keywords=["analytics"], tech_stack=["dbt"], locations=["Remote"])
    good = CompanyCandidate(
        name="X",
        domain="x.io",
        source="hn",
        source_url="u",
        description="analytics on dbt",
        locations=["REMOTE (US)"],
    )
    bad = CompanyCandidate(name="Y", domain="y.io", source="hn", source_url="u")
    assert icp_score(icp, good) == 4.0
    assert icp_score(icp, bad) == 0.0


@respx.mock
async def test_prospector_filters_team_size_and_limits(tmp_path: Path) -> None:
    mount_site(respx.mock, "acme-analytics.io")
    icp = _icp(
        team_size={"min": 10, "max": 500},
        sources={
            "csv_path": str(FIX / "leads_salesnav.csv"),
            "websites": ["https://acme-analytics.io"],
        },
    )
    f = PoliteFetcher(make_settings(tmp_path))
    out = await gather(icp, f)
    assert [c.domain for c in out] == ["acme-analytics.io"]  # bigcorp too large, deduped
    assert len(out[0].people) == 2
