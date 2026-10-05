"""MX lookup with dnspython. No SMTP connection is ever opened (no RCPT probing)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver

from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import set_attrs, span


@dataclass
class MXResult:
    domain: str
    hosts: list[str] = field(default_factory=list)
    implicit: bool = False  # no MX, but an A record exists (RFC 5321 §5.1 implicit MX)
    null_mx: bool = False  # RFC 7505 "." = domain explicitly accepts no mail
    error: str | None = None

    @property
    def accepts_mail(self) -> bool:
        return bool(self.hosts) and not self.null_mx


class MXResolver(Protocol):
    async def lookup(self, domain: str) -> MXResult: ...


class DNSResolver:
    """Live DNS via dnspython's async resolver, cached per process."""

    def __init__(self, timeout_s: float = 4.0) -> None:
        self._resolver = dns.asyncresolver.Resolver()
        self._resolver.lifetime = timeout_s
        self._cache: dict[str, MXResult] = {}

    async def lookup(self, domain: str) -> MXResult:
        domain = domain.lower().rstrip(".")
        if domain in self._cache:
            return self._cache[domain]
        with span("tool.dns_mx", **{"pp.domain": domain}) as s:
            started = time.perf_counter()
            res = await self._lookup(domain)
            metrics.TOOL_CALLS.labels("dns_mx", "ok" if res.error is None else "error").inc()
            metrics.TOOL_LATENCY.labels("dns_mx").observe(time.perf_counter() - started)
            set_attrs(
                s, **{"pp.mx_hosts": ",".join(res.hosts), "pp.implicit": res.implicit,
                      "pp.error": res.error}
            )  # fmt: skip
        self._cache[domain] = res
        return res

    async def _lookup(self, domain: str) -> MXResult:
        try:
            answer = await self._resolver.resolve(domain, "MX")
            records = sorted(
                ((r.preference, str(r.exchange).rstrip(".").lower()) for r in answer),
                key=lambda t: t[0],
            )
            hosts = [h for _, h in records]
            if hosts == [""]:
                return MXResult(domain, [], null_mx=True)
            return MXResult(domain, hosts)
        except dns.resolver.NXDOMAIN:
            return MXResult(domain, error="NXDOMAIN")
        except dns.resolver.NoAnswer:
            try:
                await self._resolver.resolve(domain, "A")
                return MXResult(domain, [domain], implicit=True)
            except dns.exception.DNSException:
                return MXResult(domain, error="no MX and no A record")
        except dns.exception.DNSException as exc:
            return MXResult(domain, error=type(exc).__name__)


class StaticResolver:
    """Deterministic resolver for tests and the offline demo (domain -> MX hosts)."""

    def __init__(self, table: dict[str, list[str]]) -> None:
        self.table = {k.lower(): v for k, v in table.items()}

    @classmethod
    def from_file(cls, path: Path) -> StaticResolver:
        return cls(json.loads(path.read_text()))

    async def lookup(self, domain: str) -> MXResult:
        domain = domain.lower()
        if domain not in self.table:
            return MXResult(domain, error="NXDOMAIN")
        hosts = self.table[domain]
        if hosts == ["."]:
            return MXResult(domain, [], null_mx=True)
        return MXResult(domain, list(hosts))
