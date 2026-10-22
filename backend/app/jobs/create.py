"""Creating a job from an approved plan revision (design 04 §5.1).

Read and check (one transaction) → ask the git host whether the user's token can push (network, none open, R1) →
re-check and insert the job (short transaction) → create its LLM key (network) → defer the pipeline.
"""

import re
import unicodedata
import uuid
from typing import Any

from fastapi import status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit
from app.config import get_settings
from app.connectors.base import ConnectorError
from app.db import get_sessionmaker
from app.enums import ApprovalSubject, JobStatus, PlanStatus, WorkItemState
from app.errors import AppError
from app.integrations import http
from app.integrations.service import credential_rows, get_credentials
from app.jobs import keys
from app.jobs.state import ACTIVE, transition
from app.llm.keys import GatewayAdminError
from app.logs import log
from app.models import Integration, JiraIssue, Job, Plan, PlanRevision, Project, User, WorkItem
from app.plans import approvals

GITLAB_PUSH_SCOPES = {"write_repository", "api"}


def slug(text: str, limit: int = 40) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:limit].strip("-") or "change"


def branch_name(key: str, title: str) -> str:
    return f"mwb/{key}-{slug(title)}"[:100]


def _conflict(message: str, field: str | None = None) -> AppError:
    extra = {"field": field} if field else {}
    return AppError(status.HTTP_409_CONFLICT, "conflict", message, **extra)


async def _checked(db: AsyncSession, user: User, work_item_id: uuid.UUID, revision_id: uuid.UUID) -> dict[str, Any]:
    item = await db.scalar(select(WorkItem).where(WorkItem.id == work_item_id, WorkItem.owner_id == user.id))
    if item is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Work item not found")
    rev = await db.get(PlanRevision, revision_id)
    plan = await db.get(Plan, rev.plan_id) if rev else None
    if rev is None or plan is None or plan.owner_id != user.id or plan.work_item_id != item.id:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Plan revision not found")
    if item.current_plan_id != plan.id or plan.status != PlanStatus.approved or rev.revision != plan.current_revision:
        raise _conflict("Only the approved, current revision of the ticket's plan can start a job.", "plan_revision_id")
    if item.state != WorkItemState.approved:
        raise _conflict(f"The ticket is {item.state.replace('_', ' ')}; a job starts from an approved ticket.")
    if not await approvals.plan_revision_approved(db, rev):
        raise _conflict("This revision's approval doesn't match its content. Approve it again.", "plan_revision_id")
    approval = await approvals.latest(db, ApprovalSubject.plan_revision, rev.id)
    active = await db.scalar(
        select(Job.id).where(Job.work_item_id == item.id, Job.status.in_([str(s) for s in ACTIVE])).limit(1)
    )
    if active is not None:
        raise _conflict("This ticket already has a job running or waiting for review.")
    project = await db.get(Project, item.project_id) if item.project_id else None
    if project is None:
        raise _conflict("The ticket has no repository.", "project_id")
    if project.status != "ready":
        raise _conflict(f"The repository {project.name} isn't ready (it's {project.status}).", "project_id")
    integration = await db.get(Integration, project.integration_id) if project.integration_id else None
    if integration is None or not integration.enabled:
        raise _conflict(f"The {project.host} integration for {project.name} isn't available.", "project_id")
    rows = await credential_rows(db, user.id, integration.id)
    if not rows:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "credential_invalid",
            f"Add your {integration.name} token first.",
            field="credential",
        )
    issue = await db.get(JiraIssue, item.jira_issue_id)
    creds = await get_credentials(db, user.id, integration)
    return {
        "item": item, "plan": plan, "rev": rev, "approval": approval, "project": project, "integration": integration,
        "creds": creds, "cred_status": {k: r.status for k, r in rows.items()}, "key": issue.key if issue else "JOB",
    }  # fmt: skip


async def check_push_access(integration: Integration, creds: dict[str, str], cred_status: dict[str, str]) -> None:
    """GitLab: the token's scopes must include write_repository and api. Gerrit can't say up front: the credential
    must have tested OK, and a rejected push later fails the job with credential_invalid."""
    if integration.kind == "gerrit":
        if any(s != "ok" for s in cred_status.values()):
            raise AppError(
                status.HTTP_409_CONFLICT, "credential_invalid",
                f"Test your {integration.name} credentials in Settings → Integrations first.", field="credential",
            )  # fmt: skip
        return
    headers = {"PRIVATE-TOKEN": creds.get("token", "")}
    try:
        async with http.client(integration.base_url, ca_pem=integration.ca_bundle, headers=headers) as c:
            resp = await c.get("/api/v4/personal_access_tokens/self")
    except Exception as exc:  # noqa: BLE001 - any transport problem means "can't tell"
        raise AppError(
            status.HTTP_502_BAD_GATEWAY, "internal", f"{integration.name} didn't answer: {type(exc).__name__}"
        ) from None
    if resp.status_code in (401, 403):
        raise AppError(
            status.HTTP_409_CONFLICT,
            "credential_invalid",
            f"{integration.name} rejected your token.",
            field="credential",
        )
    if resp.status_code >= 400:
        raise AppError(status.HTTP_502_BAD_GATEWAY, "internal", f"{integration.name} answered HTTP {resp.status_code}.")
    scopes = set(resp.json().get("scopes") or [])
    missing = sorted(GITLAB_PUSH_SCOPES - scopes)
    if missing:
        raise AppError(
            status.HTTP_409_CONFLICT, "credential_invalid",
            f"Your {integration.name} token can't push: it needs the {' and '.join(missing)} scope.",
            field="credential",
        )  # fmt: skip


def policy_for(doc: dict[str, Any], acknowledged: list[str]) -> dict[str, Any]:
    sandbox = doc.get("sandbox") or {}
    s = get_settings()
    return {
        "cpus": sandbox.get("cpus", 2),
        "memory_gb": sandbox.get("memory_gb", 4),
        "timeout_min": sandbox.get("timeout_min", 45),
        "max_attempts": sandbox.get("max_attempts", 3),
        "optional_hosts": [h for h in sandbox.get("optional_hosts", []) if h.lower() in s.optional_hosts],
        "max_changed_lines": s.job_max_changed_lines,
        "max_turns": s.job_max_turns,
        "acknowledged": sorted(acknowledged),
        "changes": [c["path"] for c in doc.get("changes", [])],
    }


async def create_job(user: User, work_item_id: uuid.UUID, revision_id: uuid.UUID, ip: str | None) -> uuid.UUID:
    async with get_sessionmaker()() as db:
        c = await _checked(db, user, work_item_id, revision_id)
        await db.commit()
    await check_push_access(c["integration"], c["creds"], c["cred_status"])  # network: nothing open
    rev, plan = c["rev"], c["plan"]
    async with get_sessionmaker()() as db:
        c2 = await _checked(db, user, work_item_id, revision_id)  # still true after the network call?
        job = Job(
            owner_id=user.id, work_item_id=c2["item"].id, plan_revision_id=rev.id, plan_approval_id=c2["approval"].id,
            project_id=c2["project"].id, base_branch=plan.base_branch or c2["project"].branch or "main",
            base_sha=plan.base_sha or "", branch_name=branch_name(c["key"], rev.content["title"]),
            status=JobStatus.created, policy=policy_for(rev.content, list(c2["approval"].acknowledged)),
            gateway_key_alias=keys.alias(uuid.uuid4()),
        )  # fmt: skip
        db.add(job)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise _conflict("This ticket already has a job running or waiting for review.") from None
        job.gateway_key_alias = keys.alias(job.id)
        c2["item"].state = WorkItemState.in_progress
        await audit(
            db, "job.create", user_id=user.id, target_type="job", target_id=str(job.id),
            detail={"work_item_id": str(work_item_id), "revision": rev.revision,
                    "approval_id": str(job.plan_approval_id)},
            ip=ip,
        )  # fmt: skip
        await db.commit()
        job_id, policy = job.id, job.policy
    try:
        await keys.create(job_id, user.id, policy)  # network: nothing open
    except (GatewayAdminError, ConnectorError) as exc:
        log.warning("job.key_failed", job_id=str(job_id), error=str(exc)[:200])
        await transition(job_id, JobStatus.created, JobStatus.failed, failed_stage="created", error_code="gateway",
                         error="The LLM gateway couldn't create a key for this job.")  # fmt: skip
        raise AppError(
            status.HTTP_502_BAD_GATEWAY, "llm_unavailable", "The LLM gateway couldn't create the job's key."
        ) from None
    from app.jobs.pipeline import defer_advance

    await defer_advance(job_id)
    return job_id
