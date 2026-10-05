from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import respx

from prospectpilot.sources.http import (
    OfflineMiss,
    PoliteFetcher,
    RobotsDisallowed,
    replay_path,
)

from .helpers import FIX, make_settings, mount_site


@respx.mock
async def test_robots_txt_is_respected(tmp_path: Path) -> None:
    mount_site(respx.mock, "acme-analytics.io")
    f = PoliteFetcher(make_settings(tmp_path))
    assert (await f.fetch("https://acme-analytics.io/about")).status == 200
    with pytest.raises(RobotsDisallowed):
        await f.fetch("https://acme-analytics.io/private/admin")


@respx.mock
async def test_user_agent_is_descriptive(tmp_path: Path) -> None:
    route = mount_site(respx.mock, "acme-analytics.io")
    f = PoliteFetcher(make_settings(tmp_path))
    await f.fetch("https://acme-analytics.io/")
    ua = route.calls.last.request.headers["user-agent"]
    assert ua.startswith("ProspectPilotBot/") and "robots.txt" in ua


@respx.mock
async def test_cache_avoids_second_request(tmp_path: Path) -> None:
    route = mount_site(respx.mock, "acme-analytics.io")
    f = PoliteFetcher(make_settings(tmp_path))
    first = await f.fetch("https://acme-analytics.io/careers")
    calls = route.call_count
    second = await f.fetch("https://acme-analytics.io/careers")
    assert route.call_count == calls  # served from disk cache
    assert second.from_cache and second.text == first.text


@respx.mock
async def test_rate_limit_one_request_per_interval_per_domain(tmp_path: Path) -> None:
    mount_site(respx.mock, "acme-analytics.io")
    f = PoliteFetcher(make_settings(tmp_path, http_min_interval_s=0.3))
    await f.allowed("https://acme-analytics.io/")  # robots.txt counts as a request
    t0 = time.monotonic()
    await f.fetch("https://acme-analytics.io/about")
    await f.fetch("https://acme-analytics.io/careers")
    assert time.monotonic() - t0 >= 0.55  # two throttled requests after robots.txt


async def test_offline_mode_never_touches_network(tmp_path: Path) -> None:
    f = PoliteFetcher(make_settings(tmp_path), offline=True)
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, text="x"))
        with pytest.raises(OfflineMiss):
            await f.fetch("https://example.org/")
        assert route.call_count == 0


async def test_replay_dirs_serve_recorded_sites(tmp_path: Path) -> None:
    f = PoliteFetcher(make_settings(tmp_path), offline=True, replay_dirs=[FIX / "sites"])
    res = await f.fetch("https://www.acme-analytics.io/blog/launching-pipelines-2")
    assert "Pipelines 2.0" in res.text


def test_replay_path_layout() -> None:
    root = Path("/r")
    assert replay_path(root, "https://www.x.io/") == Path("/r/x.io/index.html")
    assert replay_path(root, "https://x.io/blog/post") == Path("/r/x.io/blog/post.html")
