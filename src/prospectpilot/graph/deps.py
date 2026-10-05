"""Runtime dependencies for a pipeline run (built from Run.options)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prospectpilot.agents.mailer import Mailer
from prospectpilot.config import REPO_ROOT, Settings, get_settings
from prospectpilot.llm.client import LLM, get_llm
from prospectpilot.llm.embeddings import Embedder, get_embedder
from prospectpilot.sources.http import PoliteFetcher
from prospectpilot.verify.mx import DNSResolver, MXResolver, StaticResolver


@dataclass
class PipelineDeps:
    settings: Settings
    llm: LLM
    embedder: Embedder
    fetcher: PoliteFetcher
    resolver: MXResolver
    mailer: Mailer | None
    concurrency: int = 4

    async def aclose(self) -> None:
        await self.fetcher.aclose()


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def build_deps(
    options: dict[str, Any], settings: Settings | None = None, *, i_understand: bool = False
) -> PipelineDeps:
    """Options (all optional): offline, replay_dirs, dns_fixture, send, concurrency.

    `i_understand` is deliberately NOT an option (options come from the API); it can only be
    passed in by the CLI's --i-understand flag.
    """
    s = settings or get_settings()
    fetcher = PoliteFetcher(
        s,
        offline=bool(options.get("offline", False)),
        replay_dirs=[_resolve(d) for d in options.get("replay_dirs", [])],
    )
    resolver: MXResolver = (
        StaticResolver.from_file(_resolve(options["dns_fixture"]))
        if options.get("dns_fixture")
        else DNSResolver()
    )
    mailer = Mailer(s, i_understand=i_understand) if options.get("send", True) else None
    return PipelineDeps(
        settings=s,
        llm=get_llm() if settings is None else LLM(s),
        embedder=get_embedder() if settings is None else get_embedder(s),
        fetcher=fetcher,
        resolver=resolver,
        mailer=mailer,
        concurrency=int(options.get("concurrency", 4)),
    )
