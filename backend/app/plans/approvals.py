"""Approvals (design 03 §2, §6; rule R5): append-only decisions bound to the exact payload.

Only `record()` writes to `approvals`, and only by INSERT; the table's trigger refuses UPDATE and any DELETE
that isn't the cascade from a deleted user. `plan_revision_approved()` is what Phase 4 calls before it acts on a
plan: the revision's stored hash must match its content, and an approval must bind that hash and its
acknowledgements.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ApprovalDecision, ApprovalSubject
from app.models import Approval, PlanRevision
from app.plans.schema import approval_payload_hash, content_hash


async def record(
    db: AsyncSession,
    *,
    owner_id: int,
    subject_type: ApprovalSubject,
    subject_id: uuid.UUID,
    payload_hash: str,
    decision: ApprovalDecision,
    acknowledged: list[str] | None = None,
    comment: str | None = None,
    ip: str | None = None,
) -> Approval:
    row = Approval(
        owner_id=owner_id, subject_type=subject_type, subject_id=subject_id, payload_hash=payload_hash,
        decision=decision, acknowledged=sorted(set(acknowledged or [])), comment=comment, ip=ip,
    )  # fmt: skip
    db.add(row)
    await db.flush()
    return row


async def latest(db: AsyncSession, subject_type: ApprovalSubject, subject_id: uuid.UUID) -> Approval | None:
    return await db.scalar(
        select(Approval)
        .where(Approval.subject_type == subject_type, Approval.subject_id == subject_id)
        .order_by(Approval.decided_at.desc(), Approval.id.desc())
        .limit(1)
    )


async def plan_revision_approved(db: AsyncSession, revision: PlanRevision) -> bool:
    """True only if the revision is intact and its latest decision approves exactly it."""
    if content_hash(revision.content) != revision.content_hash:
        return False
    decision = await latest(db, ApprovalSubject.plan_revision, revision.id)
    if decision is None or decision.decision != ApprovalDecision.approved:
        return False
    return decision.payload_hash == approval_payload_hash(revision.content_hash, list(decision.acknowledged))
