"""Disposable-domain and role-address lists."""

from __future__ import annotations

from importlib import resources

ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    """
    info sales support admin administrator contact hello hi team jobs careers career hr
    recruiting recruitment talent marketing office billing accounts accounting finance invoices
    noreply no-reply donotreply do-not-reply press media pr help helpdesk enquiries enquiry
    inquiries webmaster postmaster hostmaster abuse security privacy legal compliance partners
    partnerships bizdev ops operations it devops engineering dev feedback newsletter news
    service customerservice customers orders shop store reception general mail email all staff
    founders investors ir community events
    """.split()
)


def _load_disposable() -> frozenset[str]:
    text = resources.files("prospectpilot.verify").joinpath("disposable_domains.txt").read_text()
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip() and line[0] != "#"
    )


DISPOSABLE_DOMAINS = _load_disposable()


def is_disposable(domain: str) -> bool:
    domain = domain.lower()
    parts = domain.split(".")
    # match the domain or any parent (e.g. foo.mailinator.com)
    return any(".".join(parts[i:]) in DISPOSABLE_DOMAINS for i in range(len(parts) - 1))


def is_role(local: str) -> bool:
    base = local.lower().split("+", 1)[0]
    return base in ROLE_LOCAL_PARTS or base.rstrip("0123456789") in ROLE_LOCAL_PARTS
