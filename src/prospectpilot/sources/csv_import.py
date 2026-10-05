"""CSV import shaped like a Sales Navigator lead export (the user's own export, not scraped)."""

from __future__ import annotations

import csv
from pathlib import Path

from prospectpilot.models import CompanyCandidate, PersonCandidate

# Accept the common header spellings of a Sales Navigator / CRM export.
_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("First Name", "first_name", "FirstName"),
    "last_name": ("Last Name", "last_name", "LastName"),
    "title": ("Title", "Job Title", "title"),
    "company": ("Company", "Company Name", "Account Name", "company"),
    "website": ("Company Website", "Website", "Company Domain", "Domain", "website"),
    "email": ("Email", "Email Address", "email"),
    "location": ("Location", "Geography", "Company Location", "location"),
    "industry": ("Industry", "Company Industry", "industry"),
    "headcount": ("Company Headcount", "Employees", "Company Size", "headcount"),
}


def _get(row: dict[str, str], key: str) -> str:
    for alias in _ALIASES[key]:
        if row.get(alias):
            return row[alias].strip()
    return ""


def _headcount(value: str) -> int | None:
    value = value.replace(",", "")
    digits = "".join(ch if ch.isdigit() else " " for ch in value).split()
    if not digits:
        return None
    nums = [int(d) for d in digits]
    return max(nums) if len(nums) == 1 else (nums[0] + nums[1]) // 2


def load_csv(path: str | Path) -> list[CompanyCandidate]:
    by_domain: dict[str, CompanyCandidate] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            website = _get(row, "website")
            company = _get(row, "company")
            if not website or not company:
                continue
            first, last = _get(row, "first_name"), _get(row, "last_name")
            person = PersonCandidate(
                full_name=f"{first} {last}".strip() or None,
                title=_get(row, "title") or None,
                email=_get(row, "email") or None,
                source_url=f"csv:{Path(path).name}",
            )
            cand = CompanyCandidate(
                name=company,
                domain=website,
                source="csv",
                source_url=f"csv:{Path(path).name}",
                description=_get(row, "industry"),
                locations=[loc] if (loc := _get(row, "location")) else [],
                team_size=_headcount(_get(row, "headcount")),
            )
            existing = by_domain.setdefault(cand.domain, cand)
            if person.full_name:
                existing.people.append(person)
    return list(by_domain.values())
