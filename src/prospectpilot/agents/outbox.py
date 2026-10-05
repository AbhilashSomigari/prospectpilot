"""Outbox: deliver due messages to the Mailpit sandbox, honour replies, record message ids."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from prospectpilot.agents.mailer import Mailer, build_message
from prospectpilot.config import Settings
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import OutboxMessage, Prospect
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import set_attrs, span

log = logging.getLogger(__name__)

STOP_STATUSES = frozenset({"sim_reply", "sim_objection", "replied", "unsubscribed"})


async def flush_due(
    mailer: Mailer,
    settings: Settings,
    *,
    now: datetime | None = None,
    prospect_ids: list[int] | None = None,
) -> int:
    """Send every scheduled message whose time has come. Returns the number sent."""
    now = now or datetime.now(UTC)
    sent = 0
    with span("agent.outbox.flush", **{"pp.now": now.isoformat()}) as s:
        async with session_scope() as session:
            stmt = (
                select(OutboxMessage, Prospect)
                .join(Prospect, Prospect.id == OutboxMessage.prospect_id)
                .where(OutboxMessage.status == "scheduled", OutboxMessage.scheduled_for <= now)
                .order_by(OutboxMessage.scheduled_for, OutboxMessage.step)
                .with_for_update(of=OutboxMessage, skip_locked=True)
            )
            if prospect_ids is not None:
                stmt = stmt.where(OutboxMessage.prospect_id.in_(prospect_ids))
            rows = (await session.execute(stmt)).all()
            for msg, prospect in rows:
                if prospect.status in STOP_STATUSES:
                    msg.status = "cancelled"
                    msg.error = f"prospect status {prospect.status}"
                    metrics.EMAILS.labels("cancelled").inc()
                    continue
                email = build_message(
                    sender=settings.mail_from,
                    to=msg.to_email,
                    subject=msg.subject,
                    body=msg.body,
                    headers={
                        "X-ProspectPilot-Prospect": str(prospect.id),
                        "X-ProspectPilot-Step": str(msg.step),
                    },
                )
                try:
                    msg.smtp_message_id = await mailer.send(email)
                    msg.status = "sent"
                    msg.sent_at = datetime.now(UTC)
                    sent += 1
                    metrics.EMAILS.labels("sent").inc()
                except Exception as exc:
                    log.warning("send failed for outbox %s: %s", msg.id, exc)
                    msg.status = "failed"
                    msg.error = str(exc)[:500]
                    metrics.EMAILS.labels("failed").inc()
        set_attrs(s, **{"pp.sent": sent, "pp.considered": len(rows)})
    return sent


async def cancel_followups(prospect_id: int) -> None:
    async with session_scope() as session:
        for msg in await session.scalars(
            select(OutboxMessage).where(
                OutboxMessage.prospect_id == prospect_id, OutboxMessage.status == "scheduled"
            )
        ):
            msg.status = "cancelled"
            msg.error = "prospect replied (simulated)"
