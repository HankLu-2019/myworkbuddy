"""CI failure → Jira ticket → work item (design 08 §F6).

`draft()` proposes the issue for one finding of a `ci-failures` run: every text field editable. `create()` binds the
edited draft to an approval (rule R5), makes the issue in one attempt after looking for one carrying the finding's
label (a crash between Jira's answer and our write then finds it instead of making a second), records it in
`finding_tickets`, and adds the work item; with `autoplan`, the plan starts too.
"""

import hashlib
import re
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit
from app.connectors import jira
from app.connectors.base import ConnectorError, connect
from app.db import get_sessionmaker
from app.enums import ApprovalDecision, ApprovalSubject
from app.errors import AppError
from app.inbox.email import app_url
from app.models import FindingTicket, Integration, JiraIssue, Project, Run, User, WorkItem
from app.plans import approvals
from app.plans.schema import canonical_json
from app.plans.work_items import create_work_item, jira_integrations

TICKET_AGENTS = {"ci-failures"}
SUMMARY_CHARS = 255
DESCRIPTION_CHARS = 20_000
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,19}$")
LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class TicketIn(BaseModel):
    fingerprint: str = Field(min_length=1, max_length=200)
    integration_id: int
    project_key: str = Field(pattern=PROJECT_KEY.pattern)
    issue_type: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=SUMMARY_CHARS)
    description: str = Field(max_length=DESCRIPTION_CHARS)
    labels: list[str] = Field([], max_length=10)
    autoplan: bool = False
    repository_id: int | None = None


def fp_label(owner_id: int, fingerprint: str) -> str:
    return "mwb-fp-" + hashlib.sha1(f"{owner_id}:{fingerprint}".encode()).hexdigest()[:10]  # noqa: S324 - an id


def subject_id(owner_id: int, fingerprint: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"mwb:finding:{owner_id}:{fingerprint}")


async def _run_and_finding(db: AsyncSession, run_id: uuid.UUID, fingerprint: str, user: User):
    run = await db.get(Run, run_id)
    if run is None or run.owner_id != user.id:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Run not found")
    if run.config.get("agent_slug") not in TICKET_AGENTS:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "Tickets can be made from CI failure runs only.")
    finding = next((f for f in (run.report or {}).get("findings", []) if f.get("fingerprint") == fingerprint), None)
    if finding is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "That finding isn't in this run.")
    return run, finding


async def _existing(db: AsyncSession, user: User, fingerprint: str) -> dict[str, Any] | None:
    row = await db.scalar(
        select(FindingTicket).where(FindingTicket.owner_id == user.id, FindingTicket.fingerprint == fingerprint)
    )
    return {"jira_key": row.jira_key, "work_item_id": row.work_item_id} if row else None


async def _guess_repository(db: AsyncSession, user: User, finding: dict[str, Any]) -> int | None:
    """The user's ready repository whose path appears in the fingerprint or a link (team/service)."""
    hay = " ".join([finding.get("fingerprint", ""), *map(str, finding.get("links") or [])])
    projects = await db.scalars(select(Project).where(Project.owner_id == user.id, Project.status == "ready"))
    for p in projects:
        path = urlparse(p.repo_url).path.strip("/").removesuffix(".git")
        tail = "/".join(path.split("/")[-2:])
        if tail and tail in hay:
            return p.id
    return None


def _description(run: Run, finding: dict[str, Any]) -> str:
    parts = [str(finding.get("detail") or "").strip()]
    if finding.get("links"):
        parts.append("Links:\n" + "\n".join(f"- {link}" for link in finding["links"]))
    parts.append(
        f"Found by MyWorkBuddy's {run.config.get('name') or 'CI failures'} check: {app_url(f'/runs/{run.id}')}"
    )
    return "\n\n".join(p for p in parts if p)[:DESCRIPTION_CHARS]


async def draft(db: AsyncSession, run_id: uuid.UUID, fingerprint: str, user: User) -> dict[str, Any]:
    run, finding = await _run_and_finding(db, run_id, fingerprint, user)
    existing = await _existing(db, user, fingerprint)
    integrations = await jira_integrations(db, user.id)
    repository_id = await _guess_repository(db, user, finding)
    out: dict[str, Any] = {
        "fingerprint": fingerprint, "existing": existing,
        "integrations": [{"id": i.id, "name": i.name} for i in integrations],
        "integration_id": integrations[0].id if integrations else None, "projects": [], "project_key": None,
        "issue_type": "Bug", "summary": str(finding.get("title") or "CI failure")[:SUMMARY_CHARS],
        "description": _description(run, finding), "labels": ["mwb-ci"],
        "autoplan": False, "repository_id": repository_id,
    }  # fmt: skip
    names = {i.id: i.name for i in integrations}
    last_key = await db.scalar(
        select(JiraIssue.key).where(JiraIssue.owner_id == user.id).order_by(JiraIssue.fetched_at.desc()).limit(1)
    )
    await db.commit()
    if existing or not integrations:
        return out
    try:  # network: nothing open (R1)
        out["projects"] = await jira.projects(await connect(user.id, "jira", names[out["integration_id"]]))
    except ConnectorError as exc:
        out["projects_error"] = exc.message
    if last_key and any(p["key"] == last_key.split("-")[0] for p in out["projects"]):
        out["project_key"] = last_key.split("-")[0]
    return out


def payload(body: TicketIn, owner_id: int) -> dict[str, Any]:
    """What is approved and sent: the fields of the issue, with the finding's label always added."""
    labels = sorted({*(label for label in body.labels if LABEL.fullmatch(label)), fp_label(owner_id, body.fingerprint)})
    return {
        "integration_id": body.integration_id, "project_key": body.project_key, "issue_type": body.issue_type,
        "summary": body.summary.strip(), "description": body.description.strip(), "labels": labels,
    }  # fmt: skip


async def create(db: AsyncSession, run_id: uuid.UUID, body: TicketIn, user: User, ip: str | None) -> dict[str, Any]:
    run, _finding = await _run_and_finding(db, run_id, body.fingerprint, user)
    if existing := await _existing(db, user, body.fingerprint):
        raise AppError(status.HTTP_409_CONFLICT, "conflict", f"This finding already has {existing['jira_key']}.",
                       jira_key=existing["jira_key"])  # fmt: skip
    integration = await db.get(Integration, body.integration_id)
    if (
        integration is None
        or integration.kind != "jira"
        or integration.id not in {i.id for i in await jira_integrations(db, user.id)}
    ):
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "Choose a Jira you have credentials for.",
                       field="integration_id")  # fmt: skip
    if body.autoplan and body.repository_id is None:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
                       "Choose the repository to plan against.", field="repository_id")  # fmt: skip
    data = payload(body, user.id)
    digest = hashlib.sha256(canonical_json(data).encode()).hexdigest()
    approval = await approvals.record(
        db, owner_id=user.id, subject_type=ApprovalSubject.jira_issue, subject_id=subject_id(user.id, body.fingerprint),
        payload_hash=digest, decision=ApprovalDecision.approved, ip=ip,
    )  # fmt: skip
    await audit(db, "jira_issue.approve", user_id=user.id, target_type="run", target_id=str(run.id),
                detail={"approval_id": str(approval.id), "fingerprint": body.fingerprint}, ip=ip)  # fmt: skip
    name, approval_id = integration.name, approval.id
    await db.commit()

    conn = await connect(user.id, "jira", name)  # network from here, nothing open (R1)
    try:
        key = await jira.find_by_label(conn, fp_label(user.id, body.fingerprint))
        created = key is None
        if key is None:
            key = await jira.create_issue(conn, {
                "project": {"key": data["project_key"]}, "issuetype": {"name": data["issue_type"]},
                "summary": data["summary"], "description": data["description"], "labels": data["labels"],
            })  # fmt: skip
    except ConnectorError as exc:
        code = "credential_invalid" if exc.code == "auth_failed" else "internal"
        raise AppError(status.HTTP_502_BAD_GATEWAY, code, f"Jira didn't create the issue: {exc.message}") from None
    finally:
        await conn.close()

    async with get_sessionmaker()() as s:
        await s.execute(
            insert(FindingTicket).values(owner_id=user.id, fingerprint=body.fingerprint, jira_key=key, run_id=run.id)
            .on_conflict_do_nothing(index_elements=["owner_id", "fingerprint"])
        )  # fmt: skip
        await audit(s, "jira_issue.create", user_id=user.id, target_type="jira_issue", target_id=key,
                    detail={"approval_id": str(approval_id), "found": not created}, ip=ip)  # fmt: skip
        await s.commit()

    try:
        item_id = await create_work_item(user, key, body.repository_id, body.integration_id)
    except AppError as exc:
        if exc.status_code != status.HTTP_409_CONFLICT or exc.code != "conflict":
            raise
        async with get_sessionmaker()() as s:  # already on the board
            item_id = await s.scalar(
                select(WorkItem.id).join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
                .where(WorkItem.owner_id == user.id, JiraIssue.key == key)
            )  # fmt: skip
    plan_started = False
    async with get_sessionmaker()() as s:
        row = await s.scalar(
            select(FindingTicket).where(
                FindingTicket.owner_id == user.id, FindingTicket.fingerprint == body.fingerprint
            )
        )
        if row is not None:
            row.work_item_id = item_id
        if body.autoplan and item_id is not None:
            from app.plans.planner import defer_draft, start_plan

            item = await s.get(WorkItem, item_id)
            if item is not None and item.state == "new" and item.project_id:
                plan = await start_plan(s, item)
                await s.commit()
                await defer_draft(plan.id, item.project_id)
                plan_started = True
        await s.commit()
    jira_base = integration.base_url.rstrip("/")
    return {"jira_key": key, "jira_url": f"{jira_base}/browse/{key}", "work_item_id": item_id,
            "plan_started": plan_started, "created": created}  # fmt: skip
