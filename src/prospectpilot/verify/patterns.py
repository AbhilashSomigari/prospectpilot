"""Email pattern generation and inference from known addresses at the same domain."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

# Heuristic prior over B2B address formats, used ONLY when a domain has no known addresses to
# learn from. These are hand-set ranking weights, not measured statistics; they just make the
# ordering of guesses sensible (first.last before last.first) and keep unproven guesses low.
PATTERN_PRIOR: dict[str, float] = {
    "first.last": 0.40,
    "first": 0.30,
    "flast": 0.30,
    "firstlast": 0.25,
    "f.last": 0.20,
    "first_last": 0.15,
    "firstl": 0.15,
    "first-last": 0.10,
    "last.first": 0.10,
    "last": 0.10,
}

_Builder = Callable[[str, str], str]
PATTERNS: dict[str, _Builder] = {
    "first.last": lambda f, last: f"{f}.{last}",
    "first": lambda f, last: f,
    "flast": lambda f, last: f"{f[0]}{last}",
    "firstlast": lambda f, last: f"{f}{last}",
    "f.last": lambda f, last: f"{f[0]}.{last}",
    "first_last": lambda f, last: f"{f}_{last}",
    "firstl": lambda f, last: f"{f}{last[0]}",
    "first-last": lambda f, last: f"{f}-{last}",
    "last.first": lambda f, last: f"{last}.{f}",
    "last": lambda f, last: last,
}

_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "jr", "sr", "ii", "iii", "phd", "md"}


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


@dataclass(frozen=True)
class NameParts:
    first: str
    last: str


def split_name(full_name: str) -> NameParts | None:
    tokens = [t for t in re.split(r"[\s,]+", full_name.strip()) if _ascii(t) not in _HONORIFICS]
    tokens = [t for t in tokens if not re.fullmatch(r"\(.*\)|\".*\"", t)]
    if len(tokens) < 2:
        return None
    first, last = _ascii(tokens[0]), _ascii(tokens[-1])
    if not first or not last:
        return None
    return NameParts(first, last)


def local_parts(name: NameParts) -> dict[str, str]:
    return {p: build(name.first, name.last) for p, build in PATTERNS.items()}


def match_patterns(local: str, name: NameParts) -> list[str]:
    """Which patterns would have produced `local` for this person."""
    local = local.lower().split("+", 1)[0]
    return [p for p, lp in local_parts(name).items() if lp == local]


@dataclass
class DomainPattern:
    pattern: str
    evidence: int  # how many known addresses at the domain fit this pattern
    total_known: int


def infer_domain_pattern(known: list[tuple[str, str]]) -> DomainPattern | None:
    """Infer the domain's format from (full_name, email) pairs that are known to be real.

    Ambiguous matches (e.g. a one-letter first name) count for every pattern they fit; the
    pattern with the most support wins, ties broken by the prior.
    """
    votes: Counter[str] = Counter()
    usable = 0
    for full_name, email in known:
        name = split_name(full_name)
        if name is None or "@" not in email:
            continue
        hits = match_patterns(email.split("@", 1)[0], name)
        if hits:
            usable += 1
            votes.update(hits)
    if not votes:
        return None
    best = max(votes, key=lambda p: (votes[p], PATTERN_PRIOR[p]))
    return DomainPattern(best, votes[best], usable)
