"""Pragmatic email syntax validation (the RFC 5321/5322 subset real mailboxes use)."""

from __future__ import annotations

import re

_LOCAL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$", re.I)
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", re.I)
_TLD = re.compile(r"^[a-z]{2,63}$", re.I)


def check_syntax(email: str) -> tuple[bool, str]:
    if not email or email != email.strip():
        return False, "empty or surrounded by whitespace"
    if len(email) > 254:
        return False, "longer than 254 characters"
    if email.count("@") != 1:
        return False, "must contain exactly one @"
    local, domain = email.rsplit("@", 1)
    if not local or len(local) > 64:
        return False, "local part empty or longer than 64 characters"
    if not _LOCAL.match(local):
        return False, "local part has invalid characters or dots"
    labels = domain.split(".")
    if len(labels) < 2:
        return False, "domain has no TLD"
    if not all(_LABEL.match(label) for label in labels):
        return False, "domain label invalid"
    if not _TLD.match(labels[-1]):
        return False, "TLD invalid"
    return True, "syntax ok"


def split(email: str) -> tuple[str, str]:
    local, domain = email.rsplit("@", 1)
    return local.lower(), domain.lower()
