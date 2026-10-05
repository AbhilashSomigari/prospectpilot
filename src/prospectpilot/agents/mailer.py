"""SMTP delivery — sandbox only.

The guard below is intentionally hard-coded: mail may only go to the local Mailpit sandbox.
Any other host is refused unless BOTH `ALLOW_REAL_SEND=true` is configured AND the caller passes
`i_understand=True` (the CLI's `--i-understand` flag). Nothing in this repo sets either.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid

from prospectpilot.config import Settings
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import set_attrs, span

SANDBOX_SMTP_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "mailpit"})


class RealSendRefused(RuntimeError):
    pass


def ensure_sandbox(host: str, *, allow_real_send: bool, i_understand: bool) -> None:
    if host.strip().lower() in SANDBOX_SMTP_HOSTS:
        return
    if allow_real_send and i_understand:
        return
    raise RealSendRefused(
        f"refusing to send via SMTP host {host!r}: only the Mailpit sandbox "
        f"({', '.join(sorted(SANDBOX_SMTP_HOSTS))}) is allowed. Real sending requires "
        "ALLOW_REAL_SEND=true AND the --i-understand flag."
    )


def build_message(
    *, sender: str, to: str, subject: str, body: str, headers: dict[str, str]
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain="prospectpilot.test")
    for k, v in headers.items():
        msg[k] = v
    msg.set_content(body)
    return msg


class Mailer:
    def __init__(self, settings: Settings, *, i_understand: bool = False) -> None:
        ensure_sandbox(
            settings.smtp_host,
            allow_real_send=settings.allow_real_send,
            i_understand=i_understand,
        )
        self.settings = settings
        self.i_understand = i_understand

    def _send_sync(self, msg: EmailMessage) -> None:
        # re-check at send time so a mutated config can never bypass the guard
        ensure_sandbox(
            self.settings.smtp_host,
            allow_real_send=self.settings.allow_real_send,
            i_understand=self.i_understand,
        )
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(msg)

    async def send(self, msg: EmailMessage) -> str:
        with span(
            "tool.smtp_send",
            **{
                "pp.smtp_host": self.settings.smtp_host,
                "pp.to_domain": str(msg["To"]).split("@")[-1],
            },
        ) as s:
            started = time.perf_counter()
            try:
                await asyncio.to_thread(self._send_sync, msg)
                status = "sent"
            except Exception:
                status = "error"
                raise
            finally:
                metrics.TOOL_CALLS.labels("smtp_send", status).inc()
                metrics.TOOL_LATENCY.labels("smtp_send").observe(time.perf_counter() - started)
            set_attrs(s, **{"pp.message_id": str(msg["Message-ID"])})
            return str(msg["Message-ID"])
