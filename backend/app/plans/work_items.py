"""Jira issues and work items (design 03 §1, §2, §7).

Every Jira call happens with no transaction open (rule R1): fetch first, then a short transaction upserts the
cache and creates work items. Work items are private to their owner; nothing here reads another user's rows.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import jira
from app.connectors.base import ConnectorError, connect
from app.db import get_sessionmaker
from app.errors import AppError
from app.integrations.service import credential_rows
from app.logs import log
from app.models import Integration, JiraIssue, Project, User, WorkItem

DEFAULT_JQL = "assignee = currentUser() AND resolution = Unresolved"


def parse_jira_time(value: str | None) -> datetime:
    """Jira's "2026-10-20T09:12:00.000+0200" (or ISO with a colon); now when missing."""
    if not value:
        return datetime.now(UTC)
    text = value.replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(UTC)


async def jira_integrations(db: AsyncSession, user_id: int) -> list[Integration]:
    """Enabled Jira integrations the user has saved credentials for."""
    rows = await db.scalars(
        select(Integration).where(Integration.kind == "jira", Integration.enabled).order_by(Integration.name)
    )
    return [i for i in rows if await credential_rows(db, user_id, i.id)]


async def upsert_issue(db: AsyncSession, owner_id: int, integration_id: int, data: dict[str, Any]) -> int:
    values = {
        "owner_id": owner_id,
        "integration_id": integration_id,
        "key": data["key"],
        "summary": (data.get("summary") or "")[:1000],
        "status": data.get("status") or "",
        "issue_type": data.get("type"),
        "priority": data.get("priority"),
        "updated_remote": parse_jira_time(data.get("updated")),
        "fields": {k: data.get(k) for k in ("description", "labels", "components", "links", "assignee")},
        "fetched_at": func.now(),
    }
    stmt = insert(JiraIssue).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[JiraIssue.owner_id, JiraIssue.integration_id, JiraIssue.key],
        set_={k: getattr(stmt.excluded, k) for k in values if k not in ("owner_id", "integration_id", "key")},
    ).returning(JiraIssue.id)
    return int((await db.execute(stmt)).scalar_one())


async def guess_project(db: AsyncSession, user: User, names: list[str]) -> int | None:
    """A project whose name equals a Jira component or label (case-insensitive), when exactly one matches.
    Only repositories the user can see: their own and the team's."""
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    if not wanted:
        return None
    ids = list(
        await db.scalars(
            select(Project.id).where(
                func.lower(Project.name).in_(wanted),
                or_(Project.owner_id == user.id, Project.visibility == "public"),
            )
        )
    )
    return ids[0] if len(ids) == 1 else None


@dataclass
class IntakeResult:
    issues: int = 0
    created: list[uuid.UUID] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # integration names


async def run_intake(user_id: int) -> IntakeResult:
    """One user's intake (design 03 §7 steps 1–3). Auto-drafting (step 4) is added by the planner (P3)."""
    result = IntakeResult()
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
        if user is None or not user.is_active or not user.jira_intake_enabled:
            return result
        jql = user.jira_intake_jql or DEFAULT_JQL
        integrations = [(i.id, i.name) for i in await jira_integrations(db, user_id)]
    for integration_id, name in integrations:
        try:
            conn = await connect(user_id, "jira", name)
            issues = await jira.fetch_issues(conn, jql)
        except ConnectorError as exc:
            result.failed.append(name)
            log.warning("jira_intake.failed", user_id=user_id, integration=name, code=exc.code)
            continue
        async with get_sessionmaker()() as db:
            user = await db.get(User, user_id)
            assert user is not None
            for data in issues:
                issue_id = await upsert_issue(db, user_id, integration_id, data)
                result.issues += 1
                exists = await db.scalar(
                    select(WorkItem.id).where(WorkItem.owner_id == user_id, WorkItem.jira_issue_id == issue_id).limit(1)
                )
                if exists is None:
                    project_id = await guess_project(
                        db, user, (data.get("components") or []) + (data.get("labels") or [])
                    )
                    item = WorkItem(owner_id=user_id, jira_issue_id=issue_id, project_id=project_id)
                    db.add(item)
                    await db.flush()
                    result.created.append(item.id)
            await db.commit()
    return result


async def create_work_item(user: User, key: str, project_id: int | None, integration_id: int | None) -> uuid.UUID:
    """`POST /api/work-items`: fetch the issue (no transaction open), then cache it and create the item."""
    key = key.strip().upper()
    if not jira.KEY.fullmatch(key):
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Enter a Jira key like PROJ-123.",
            field="jira_key",
        )
    async with get_sessionmaker()() as db:
        integrations = await jira_integrations(db, user.id)
        if integration_id is not None:
            integrations = [i for i in integrations if i.id == integration_id]
        if not integrations:
            raise AppError(
                status.HTTP_409_CONFLICT, "conflict", "Add your Jira credentials in Settings → Integrations first.",
                field="integration_id",
            )  # fmt: skip
        if len(integrations) > 1:
            raise AppError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "Choose which Jira the ticket is in.",
                field="integration_id",
            )  # fmt: skip
        integration_id, name = integrations[0].id, integrations[0].name
        if project_id is not None:
            await visible_project(db, user, project_id)
        await db.commit()
    try:
        data = await jira.fetch_issue(await connect(user.id, "jira", name), key)
    except ConnectorError as exc:
        code = {"not_found": status.HTTP_404_NOT_FOUND, "auth_failed": status.HTTP_409_CONFLICT}.get(exc.code, 502)
        err = {"not_found": "not_found", "auth_failed": "credential_invalid"}.get(exc.code, "internal")
        raise AppError(code, err, f"{key}: {exc.message}") from None
    async with get_sessionmaker()() as db:
        issue_id = await upsert_issue(db, user.id, integration_id, data)
        existing = await db.scalar(
            select(WorkItem.id).where(
                WorkItem.owner_id == user.id,
                WorkItem.jira_issue_id == issue_id,
                WorkItem.project_id.is_(None) if project_id is None else WorkItem.project_id == project_id,
            )
        )
        if existing is not None:
            raise AppError(
                status.HTTP_409_CONFLICT, "conflict", f"{key} is already on your board.", work_item_id=str(existing)
            )
        item = WorkItem(owner_id=user.id, jira_issue_id=issue_id, project_id=project_id)
        db.add(item)
        await db.commit()
        return item.id


async def visible_project(db: AsyncSession, user: User, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if project is None or not (project.owner_id == user.id or project.visibility == "public"):
        raise AppError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_error",
            "Choose one of your repositories.",
            field="project_id",
        )
    return project
