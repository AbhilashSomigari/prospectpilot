"""Polite HTTP fetching: robots.txt, 1 req/s per domain, descriptive User-Agent, on-disk cache.

Modes:
- online (default): cache-first, then network.
- offline: cache/replay only; a miss raises `OfflineMiss`. Used by the demo, evals and CI.

`replay_dirs` hold pre-recorded sites laid out as `<dir>/<domain>/<path>.html` (path "/" ->
`index.html`) and are consulted before the cache, so fixture companies resolve with no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from prospectpilot.config import Settings, get_settings
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import set_attrs, span


class FetchError(RuntimeError):
    pass


class RobotsDisallowed(FetchError):
    pass


class OfflineMiss(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    content_type: str
    fetched_at: datetime
    from_cache: bool

    def json(self) -> Any:
        return json.loads(self.text)


def _domain(url: str) -> str:
    return urlsplit(url).netloc.lower()


def replay_path(root: Path, url: str) -> Path:
    parts = urlsplit(url)
    path = parts.path.strip("/") or "index"
    if parts.query:
        path += "__" + hashlib.sha1(parts.query.encode()).hexdigest()[:10]
    if not Path(path).suffix:
        path += ".html"
    return root / parts.netloc.lower().removeprefix("www.") / path


class PoliteFetcher:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        offline: bool = False,
        replay_dirs: list[Path] | None = None,
        cache_ttl: timedelta = timedelta(days=7),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.offline = offline
        self.replay_dirs = replay_dirs or []
        self.cache_dir = Path(self.settings.http_cache_dir)
        self.cache_ttl = cache_ttl
        self.min_interval = self.settings.http_min_interval_s
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.settings.http_user_agent},
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ cache
    def _cache_file(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def _read_cache(self, url: str) -> FetchResult | None:
        for root in self.replay_dirs:
            p = replay_path(root, url)
            if p.exists():
                ctype = "application/json" if p.suffix == ".json" else "text/html"
                mtime = datetime.fromtimestamp(p.stat().st_mtime, UTC)
                return FetchResult(url, 200, p.read_text(), ctype, mtime, True)
        f = self._cache_file(url)
        if not f.exists():
            return None
        data = json.loads(f.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if not self.offline and datetime.now(UTC) - fetched_at > self.cache_ttl:
            return None
        return FetchResult(
            url, data["status"], data["text"], data["content_type"], fetched_at, True
        )

    def _write_cache(self, res: FetchResult) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_file(res.url).write_text(
            json.dumps(
                {
                    "url": res.url,
                    "status": res.status,
                    "text": res.text,
                    "content_type": res.content_type,
                    "fetched_at": res.fetched_at.isoformat(),
                }
            )
        )

    # ----------------------------------------------------------------- polite
    async def _throttle(self, domain: str) -> None:
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            wait = self._last_hit.get(domain, 0.0) + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[domain] = time.monotonic()

    async def _get(self, url: str) -> httpx.Response:
        await self._throttle(_domain(url))
        return await self._client.get(url)

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        domain = parts.netloc.lower()
        if domain not in self._robots:
            robots_url = f"{parts.scheme}://{domain}/robots.txt"
            parser: urllib.robotparser.RobotFileParser | None = None
            cached = self._read_cache(robots_url)
            text: str | None = None
            status = 404
            if cached is not None:
                text, status = cached.text, cached.status
            elif not self.offline:
                try:
                    r = await self._get(robots_url)
                    status, text = r.status_code, r.text
                    self._write_cache(
                        FetchResult(
                            robots_url, status, text, "text/plain", datetime.now(UTC), False
                        )
                    )
                except httpx.HTTPError:
                    status, text = 599, None
            if status in (401, 403):
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
            elif status == 200 and text is not None:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
            # 404 / missing robots.txt -> everything allowed (parser None)
            self._robots[domain] = parser
        parser = self._robots[domain]
        return True if parser is None else parser.can_fetch(self.settings.http_user_agent, url)

    # ------------------------------------------------------------------ fetch
    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
        domain = _domain(url)
        with span("tool.http_fetch", **{"http.url": url, "pp.domain": domain}) as s:
            started = time.perf_counter()
            status_label = "ok"
            try:
                cached = self._read_cache(url)
                if cached is not None:
                    status_label = "cache_hit"
                    set_attrs(s, **{"pp.cache_hit": True, "http.status_code": cached.status})
                    return cached
                if self.offline:
                    status_label = "offline_miss"
                    raise OfflineMiss(url)
                if not await self.allowed(url):
                    status_label = "robots_disallowed"
                    raise RobotsDisallowed(url)
                await self._throttle(domain)
                try:
                    r = await self._client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    status_label = "error"
                    raise FetchError(f"{url}: {exc}") from exc
                res = FetchResult(
                    str(r.url),
                    r.status_code,
                    r.text,
                    r.headers.get("content-type", ""),
                    datetime.now(UTC),
                    False,
                )
                set_attrs(s, **{"pp.cache_hit": False, "http.status_code": r.status_code})
                if r.status_code >= 400:
                    status_label = f"http_{r.status_code}"
                    raise FetchError(f"{url}: HTTP {r.status_code}")
                res.url = url  # cache under the requested URL
                self._write_cache(res)
                return res
            finally:
                metrics.TOOL_CALLS.labels("http_fetch", status_label).inc()
                metrics.TOOL_LATENCY.labels("http_fetch").observe(time.perf_counter() - started)

    async def fetch_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        return (await self.fetch(url, headers=headers)).json()
