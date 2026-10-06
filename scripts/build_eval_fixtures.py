"""Freeze eval fixtures: run the real Enricher over the recorded fixture sites, store the facts.

For each company in evals/fixtures/companies.json this fetches the recorded pages (offline
replay, no network), extracts facts with the configured LLM provider (validated + grounded exactly
as in production), and writes evals/fixtures/prospects/<id>.json. Fact ids are globally unique
across fixtures (fixture n gets ids n*100+1 ...), so citing another prospect's fact is detectable.

Usage: LLM_PROVIDER=ollama uv run python scripts/build_eval_fixtures.py [--provider mock]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

from prospectpilot.agents.enricher import collect_pages, extract_facts
from prospectpilot.config import REPO_ROOT, get_settings
from prospectpilot.llm.client import LLM, track_usage
from prospectpilot.sources.http import PoliteFetcher

FIX = REPO_ROOT / "evals" / "fixtures"
SENDER = {"name": "Alex Rivera", "title": "Account Executive", "company": "ProspectPilot Demo Co."}


async def build_one(i: int, c: dict[str, Any], offers: dict[str, Any], llm: LLM,
                    fetcher: PoliteFetcher) -> dict[str, Any]:  # fmt: skip
    pages = await collect_pages(fetcher, c["domain"])
    started = time.perf_counter()
    with track_usage() as usage:
        facts = await extract_facts(llm, c["name"], pages)
    base = (i + 1) * 100
    offer = dict(offers[c["offer"]])
    offer["product"] = offer["product"]
    return {
        "id": c["domain"].removesuffix(".test"),
        "company": {
            "name": c["name"],
            "domain": c["domain"],
            "description": c["tagline"],
            "industry": c["industry"],
        },
        "person": c["persona"],
        "offer": offer,
        "sender": {**SENDER, "company": offer["product"]},
        "facts": [
            {
                "id": base + k + 1,
                "kind": f.kind,
                "text": f.text,
                "source_url": f.source_url,
                "published_at": f.published_at.isoformat() if f.published_at else None,
            }
            for k, f in enumerate(facts)
        ],
        "extraction": {
            "provider": llm.provider,
            "model": llm.model_for("extract"),
            "pages": [p.url for p in pages],
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "seconds": round(time.perf_counter() - started, 2),
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    }


async def main(provider: str | None, concurrency: int) -> None:
    spec = json.loads((FIX / "companies.json").read_text())
    llm = LLM(get_settings(), provider=provider)
    fetcher = PoliteFetcher(get_settings(), offline=True, replay_dirs=[FIX / "sites"])
    out_dir = FIX / "prospects"
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int, c: dict[str, Any]) -> None:
        async with sem:
            fx = await build_one(i, c, spec["offers"], llm, fetcher)
        (out_dir / f"{fx['id']}.json").write_text(json.dumps(fx, indent=1) + "\n")
        print(
            f"{fx['id']:<28} facts={len(fx['facts']):>2} {fx['extraction']['seconds']}s", flush=True
        )

    await asyncio.gather(*(one(i, c) for i, c in enumerate(spec["companies"])))
    await fetcher.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None)
    ap.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()
    asyncio.run(main(a.provider, a.concurrency))
