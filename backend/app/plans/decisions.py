"""Deciding on a plan (design 03 §6): approve, reject, revise with feedback, and user revisions.

Every function runs inside the caller's request transaction, locks the plan row, checks the state it read, and
leaves the commit (and the audit row, rule R3) to the route. A decision always names the revision it is about and
that revision's hash, so a stale page can never approve something its user didn't see (409 `plan_changed`).
An approval binds the hash and exactly the acknowledged warnings (R5); it never carries over to a new revision.
"""

import uuid
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connectors.base import ConnectorError
from app.enums import ApprovalDecision, ApprovalSubject, PlanStatus, RevisionAuthor, WorkItemState
from app.errors import AppError
from app.knowledge.ingest.repos import cache_dir
from app.models import Approval, JiraIssue, Plan, PlanRevision, User, WorkItem
from app.plans import approvals, repo
from app.plans.schema import PlanDoc, approval_payload_hash, content_hash
from app.plans.validation import ValidationResult, validate_plan, visible_document_ids


def _changed(
    message: str = "This plan has changed since you opened it. Reload to see the latest revision.",
) -> AppError:
    return AppError(status.HTTP_409_CONFLICT, "plan_changed", message)


async def current(db: AsyncSession, plan: Plan) -> PlanRevision | None:
    if not plan.current_revision:
        return None
    return await db.scalar(
        select(PlanRevision).where(PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.current_revision)
    )


async def _deciding(db: AsyncSession, plan: Plan, revision: int, hash_: str | None) -> tuple[PlanRevision, WorkItem]:
    """The current revision, if the plan is waiting for a decision on exactly `revision` (with `hash_`)."""
    if plan.status != PlanStatus.needs_decision:
        raise _changed(f"This plan is {plan.status.replace('_', ' ')}, so it can't be decided on now.")
    rev = await current(db, plan)
    if rev is None or rev.revision != revision or (hash_ is not None and rev.content_hash != hash_):
        raise _changed()
    if content_hash(rev.content) != rev.content_hash:  # never approve a revision that doesn't match its hash
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "This revision's content doesn't match its hash.")
    item = await db.scalar(
        select(WorkItem)
        .where(WorkItem.id == plan.work_item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert item is not None
    return rev, item


def _settle(item: WorkItem, plan: Plan, state: WorkItemState) -> None:
    if item.current_plan_id == plan.id:
        item.state, item.updated_at = state, datetime.now(UTC)


async def approve(
    db: AsyncSession, plan: Plan, *, revision: int, hash_: str, acknowledged: list[str], ip: str | None
) -> Approval:
    rev, item = await _deciding(db, plan, revision, hash_)
    result = ValidationResult.model_validate(rev.validation)
    if not result.ok:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
            "This revision has validation errors. Fix them in a new revision first.", field="validation",
        )  # fmt: skip
    acks = sorted(set(acknowledged))
    missing = [a for a in result.needs_ack if a not in acks]
    if missing:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
            "Acknowledge every warning that needs it before approving.", field="acknowledged", missing=missing,
        )  # fmt: skip
    unknown = [a for a in acks if a not in result.needs_ack]
    if unknown:
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
            "Only acknowledge the warnings this revision lists.", field="acknowledged", unknown=unknown,
        )  # fmt: skip
    row = await approvals.record(
        db, owner_id=plan.owner_id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
        payload_hash=approval_payload_hash(rev.content_hash, acks), decision=ApprovalDecision.approved,
        acknowledged=acks, ip=ip,
    )  # fmt: skip
    plan.status, plan.updated_at = PlanStatus.approved, datetime.now(UTC)
    _settle(item, plan, WorkItemState.approved)
    return row


async def reject(db: AsyncSession, plan: Plan, *, revision: int, comment: str | None, ip: str | None) -> Approval:
    rev, item = await _deciding(db, plan, revision, None)
    row = await approvals.record(
        db, owner_id=plan.owner_id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
        payload_hash=approval_payload_hash(rev.content_hash, []), decision=ApprovalDecision.rejected,
        comment=comment, ip=ip,
    )  # fmt: skip
    plan.status, plan.updated_at = PlanStatus.rejected, datetime.now(UTC)
    _settle(item, plan, WorkItemState.new)
    return row


async def revise(db: AsyncSession, plan: Plan, *, revision: int) -> WorkItem:
    """Back to `drafting`; the caller commits and defers `draft_plan` with the feedback."""
    _rev, item = await _deciding(db, plan, revision, None)
    if item.project_id is None:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "This ticket has no repository.", field="project_id")
    plan.status, plan.error, plan.updated_at = PlanStatus.drafting, None, datetime.now(UTC)
    _settle(item, plan, WorkItemState.planning)
    return item


async def validate_user_revision(
    db: AsyncSession, user: User, plan: Plan, doc: PlanDoc
) -> tuple[PlanDoc, ValidationResult]:
    """Validate a user's revision exactly like the planner's (03 §4), against the plan's base commit.

    Local git only (the ingest cache), no network; if the cache is gone, paths aren't checked and a warning says so.
    """
    item = await db.get(WorkItem, plan.work_item_id)
    assert item is not None
    issue = await db.get(JiraIssue, item.jira_issue_id)
    assert issue is not None
    existing: set[str] | None = None
    checkout = cache_dir(item.project_id) if item.project_id else None
    if checkout is not None and plan.base_sha and (checkout / ".git").exists():
        try:
            existing = await repo.tracked_paths(checkout, plan.base_sha)
        except ConnectorError:
            existing = None
    refs = await visible_document_ids(db, user, doc.knowledge_refs)
    return validate_plan(
        doc, jira_key=issue.key, existing_paths=existing, visible_refs=refs,
        optional_hosts=get_settings().optional_hosts,
    )  # fmt: skip


async def add_user_revision(
    db: AsyncSession,
    plan: Plan,
    *,
    base_revision: int,
    doc: PlanDoc,
    validation: ValidationResult,
    change_note: str | None,
) -> PlanRevision:
    """A new revision written by the user. It is never approved: any earlier approval stays with its revision."""
    if plan.status != PlanStatus.needs_decision or plan.current_revision != base_revision:
        raise _changed("This plan has changed since you started editing. Reload, then make your edits again.")
    content = doc.model_dump(mode="json")
    plan.current_revision += 1
    rev = PlanRevision(
        id=uuid.uuid4(), plan_id=plan.id, revision=plan.current_revision, content=content,
        content_hash=content_hash(content), author=RevisionAuthor.user, change_note=change_note,
        validation=validation.model_dump(),
    )  # fmt: skip
    db.add(rev)
    plan.updated_at = datetime.now(UTC)
    await db.flush()
    return rev
