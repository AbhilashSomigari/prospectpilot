"""Test helpers: serve recorded HTTP fixtures through respx (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from prospectpilot.config import Settings

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "http"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "http_cache_dir": tmp_path / "cache",
        "http_min_interval_s": 0.0,
        "otel_enabled": False,
        "llm_provider": "mock",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def site_responder(request: httpx.Request) -> httpx.Response:
    """Serve tests/fixtures/http/sites/<host>/<path>.html (robots.txt verbatim)."""
    host = request.url.host.removeprefix("www.")
    path = request.url.path.strip("/") or "index"
    root = FIX / "sites" / host
    candidates = [root / path] if path == "robots.txt" else [root / f"{path}.html"]
    for c in candidates:
        if c.exists():
            ctype = "text/plain" if c.suffix == ".txt" else "text/html"
            return httpx.Response(200, text=c.read_text(), headers={"content-type": ctype})
    return httpx.Response(404, text="not found")


def mount_site(router: respx.MockRouter, host: str) -> respx.Route:
    return router.route(host=host).mock(side_effect=site_responder)


def json_fixture(name: str) -> str:
    return (FIX / name).read_text()
