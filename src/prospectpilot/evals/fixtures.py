"""Frozen eval fixtures (evals/fixtures/prospects/*.json) → ProspectContext / Offer / Sender."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from prospectpilot.config import get_settings
from prospectpilot.models import Fact, Offer, ProspectContext, Sender


class Fixture(BaseModel):
    id: str
    company: dict[str, Any]
    person: dict[str, Any] | None
    offer: Offer
    sender: Sender
    facts: list[Fact]
    extraction: dict[str, Any] = {}

    def context(self, learnings: list[str] | None = None) -> ProspectContext:
        return ProspectContext(
            company_name=self.company["name"],
            domain=self.company["domain"],
            company_description=self.company.get("description", ""),
            person_name=self.person["name"] if self.person else None,
            person_title=self.person["title"] if self.person else None,
            facts=self.facts,
            learnings=learnings or [],
        )


def fixtures_dir() -> Path:
    return get_settings().evals_dir / "fixtures" / "prospects"


@lru_cache(maxsize=1)
def load_all() -> dict[str, Fixture]:
    out = {}
    for path in sorted(fixtures_dir().glob("*.json")):
        fx = Fixture.model_validate(json.loads(path.read_text()))
        out[fx.id] = fx
    return out


def load(fixture_id: str) -> Fixture:
    try:
        return load_all()[fixture_id]
    except KeyError:
        raise LookupError(f"unknown eval fixture {fixture_id!r}") from None
