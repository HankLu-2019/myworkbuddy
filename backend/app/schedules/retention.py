"""Daily `runs_retention` (design 02 §9): runs older than `MWB_RUNS_RETENTION_DAYS` (180) are deleted with their
transcripts; their inbox items go with them (FK cascade). Also: sent, failed and suppressed emails after 90 days,
and handled or expired schedule proposals after 30 days. Deletes run in batches, each its own short transaction.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import EmailStatus, ProposalStatus, RunStatus
from app.models import ChatSession, OutboundEmail, Run, ScheduleProposal

BATCH = 500
EMAIL_KEEP = timedelta(days=90)
PROPOSAL_KEEP = timedelta(days=30)
ACTIVE = (RunStatus.queued, RunStatus.running)


async def prune_runs(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=get_settings().runs_retention_days)
    counts = {"runs": 0, "run_sessions": 0, "emails": 0, "proposals": 0}
    maker = get_sessionmaker()
    while True:
        async with maker() as db:
            rows = (
                await db.execute(
                    select(Run.id, Run.session_id)
                    .where(Run.created_at < cutoff, Run.status.not_in(ACTIVE))
                    .order_by(Run.created_at)
                    .limit(BATCH)
                )
            ).all()
            if not rows:
                break
            sessions = [sid for _, sid in rows if sid]
            await db.execute(delete(Run).where(Run.id.in_([rid for rid, _ in rows])))
            if sessions:
                gone = await db.execute(
                    delete(ChatSession).where(ChatSession.id.in_(sessions), ChatSession.source == "run")
                )
                counts["run_sessions"] += gone.rowcount or 0  # type: ignore[attr-defined]
            counts["runs"] += len(rows)
            await db.commit()
    async with maker() as db:
        emails = await db.execute(
            delete(OutboundEmail).where(
                OutboundEmail.created_at < now - EMAIL_KEEP, OutboundEmail.status != EmailStatus.pending
            )
        )
        proposals = await db.execute(
            delete(ScheduleProposal).where(
                ScheduleProposal.created_at < now - PROPOSAL_KEEP, ScheduleProposal.status != ProposalStatus.pending
            )
        )
        counts["emails"] = emails.rowcount or 0  # type: ignore[attr-defined]
        counts["proposals"] = proposals.rowcount or 0  # type: ignore[attr-defined]
        await db.commit()
    return counts
