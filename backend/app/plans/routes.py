"""Work items API (design 03 §8). Owner only: other users' work items are 404."""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import pagination
from app.audit.service import audit
from app.deps import DB, CurrentAuth, client_ip
from app.errors import AppError
from app.models import Approval, Integration, JiraIssue, Plan, PlanRevision, Project, User, WorkItem
from app.plans.planner import defer_draft, start_plan
from app.plans.work_items import create_work_item, visible_project

router = APIRouter(prefix="/api", tags=["work items"])

State = Literal["new", "planning", "needs_decision", "approved", "in_progress", "in_review", "done", "dismissed"]


class ProjectRef(BaseModel):
    id: int
    name: str


class WorkItemOut(BaseModel):
    id: uuid.UUID
    state: State
    key: str
    summary: str
    jira_status: str
    issue_type: str | None
    priority: str | None
    labels: list[str]
    components: list[str]
    jira_url: str
    project: ProjectRef | None
    current_plan_id: uuid.UUID | None
    updated_remote: datetime
    created_at: datetime
    updated_at: datetime


class WorkItemDetail(WorkItemOut):
    description: str
    links: list[dict[str, str]]
    assignee: str | None


class WorkItemPage(BaseModel):
    items: list[WorkItemOut]
    next_cursor: str | None


class WorkItemIn(BaseModel):
    jira_key: str = Field(max_length=40)
    project_id: int | None = None
    integration_id: int | None = None


class WorkItemUpdate(BaseModel):
    project_id: int | None = None
    state: Literal["dismissed", "new"] | None = None


def _fields(w: WorkItem, issue: JiraIssue, integration: Integration, project: Project | None) -> dict:
    f = issue.fields or {}
    return {
        "id": w.id, "state": w.state, "key": issue.key, "summary": issue.summary, "jira_status": issue.status,
        "issue_type": issue.issue_type, "priority": issue.priority, "labels": f.get("labels") or [],
        "components": f.get("components") or [], "jira_url": f"{integration.base_url}/browse/{issue.key}",
        "project": ProjectRef(id=project.id, name=project.name) if project else None,
        "current_plan_id": w.current_plan_id, "updated_remote": issue.updated_remote, "created_at": w.created_at,
        "updated_at": w.updated_at,
    }  # fmt: skip


def _query(user: User):
    return (
        select(WorkItem, JiraIssue, Integration, Project)
        .join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
        .join(Integration, Integration.id == JiraIssue.integration_id)
        .outerjoin(Project, Project.id == WorkItem.project_id)
        .where(WorkItem.owner_id == user.id)
    )


async def load(db: DB, work_item_id: uuid.UUID, user: User, *, lock: bool = False) -> tuple:
    q = _query(user).where(WorkItem.id == work_item_id)
    row = (await db.execute(q.with_for_update(of=WorkItem) if lock else q)).first()
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Work item not found")
    return tuple(row)


def detail(row: tuple) -> WorkItemDetail:
    w, issue, integration, project = row
    f = issue.fields or {}
    return WorkItemDetail(
        **_fields(w, issue, integration, project), description=f.get("description") or "",
        links=f.get("links") or [], assignee=f.get("assignee"),
    )  # fmt: skip


@router.get("/work-items", response_model=WorkItemPage)
async def list_work_items(
    auth: CurrentAuth,
    db: DB,
    state: list[State] | None = Query(None),
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=100),
) -> WorkItemPage:
    q = _query(auth.user)
    q = q.where(WorkItem.state.in_(state)) if state else q.where(WorkItem.state != "dismissed")
    rows = [
        tuple(r) for r in (await db.execute(pagination.page(q, WorkItem.updated_at, WorkItem.id, cursor, limit))).all()
    ]
    page, _ = pagination.result([r[0] for r in rows], limit, created="updated_at")
    keep = {w.id for w in page}
    items = [WorkItemOut(**_fields(*r)) for r in rows if r[0].id in keep]
    last = page[-1] if len(rows) > limit else None
    return WorkItemPage(items=items, next_cursor=pagination.encode(last.updated_at, last.id) if last else None)


@router.post("/work-items", response_model=WorkItemDetail, status_code=201)
async def add_work_item(body: WorkItemIn, auth: CurrentAuth, db: DB, request: Request) -> WorkItemDetail:
    await db.commit()  # rule R1: the issue is fetched from Jira next
    item_id = await create_work_item(auth.user, body.jira_key, body.project_id, body.integration_id)
    await audit(
        db, "work_item.create", user_id=auth.user.id, target_type="work_item", target_id=str(item_id),
        detail={"jira_key": body.jira_key.strip().upper()}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    return detail(await load(db, item_id, auth.user))


@router.get("/work-items/{work_item_id}", response_model=WorkItemDetail)
async def get_work_item(work_item_id: uuid.UUID, auth: CurrentAuth, db: DB) -> WorkItemDetail:
    return detail(await load(db, work_item_id, auth.user))


@router.patch("/work-items/{work_item_id}", response_model=WorkItemDetail)
async def update_work_item(
    work_item_id: uuid.UUID, body: WorkItemUpdate, auth: CurrentAuth, db: DB, request: Request
) -> WorkItemDetail:
    w, *_ = await load(db, work_item_id, auth.user, lock=True)
    changed = body.model_dump(exclude_unset=True)
    if "project_id" in changed:
        if w.state not in ("new", "dismissed"):
            raise AppError(
                status.HTTP_409_CONFLICT, "conflict", "The repository can only change before planning starts."
            )
        if body.project_id is not None:
            await visible_project(db, auth.user, body.project_id)
        w.project_id = body.project_id
    if body.state is not None and body.state != w.state:
        allowed = {("new", "dismissed"), ("dismissed", "new")}
        if (w.state, body.state) not in allowed:
            raise AppError(
                status.HTTP_409_CONFLICT, "conflict", f"A work item in state {w.state} can't be {body.state}."
            )
        w.state = body.state
    w.updated_at = datetime.now(w.updated_at.tzinfo)
    await audit(
        db, "work_item.update", user_id=auth.user.id, target_type="work_item", target_id=str(w.id),
        detail={"fields": sorted(changed)}, ip=client_ip(request),
    )  # fmt: skip
    try:
        await db.commit()
    except IntegrityError:  # the unique (owner, issue, project) key: that pairing already exists
        await db.rollback()
        raise AppError(
            status.HTTP_409_CONFLICT, "conflict", "That ticket is already on your board for this repository."
        ) from None
    return detail(await load(db, work_item_id, auth.user))


# ---------- plans (03 §8; approval and revisions arrive with P4) ----------


class RevisionOut(BaseModel):
    revision: int
    content: dict
    content_hash: str
    author: Literal["planner", "user"]
    change_note: str | None
    validation: dict
    session_id: uuid.UUID | None
    cost_usd: float | None
    created_at: datetime


class PlanOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    status: Literal["drafting", "needs_decision", "approved", "rejected", "superseded", "failed"]
    current_revision: int
    base_sha: str | None
    base_branch: str | None
    error: str | None
    auto: bool
    revision: RevisionOut | None
    approvals: list[dict]
    created_at: datetime
    updated_at: datetime


async def plan_out(db: DB, plan: Plan) -> PlanOut:
    rev = None
    if plan.current_revision:
        r = await db.scalar(
            select(PlanRevision).where(PlanRevision.plan_id == plan.id, PlanRevision.revision == plan.current_revision)
        )
        if r is not None:
            rev = RevisionOut(
                revision=r.revision, content=r.content, content_hash=r.content_hash, author=r.author,
                change_note=r.change_note, validation=r.validation, session_id=r.session_id,
                cost_usd=float(r.cost_usd) if r.cost_usd is not None else None, created_at=r.created_at,
            )  # fmt: skip
    ids = list(await db.scalars(select(PlanRevision.id).where(PlanRevision.plan_id == plan.id)))
    decisions = []
    if ids:
        rows = await db.scalars(
            select(Approval)
            .where(Approval.subject_type == "plan_revision", Approval.subject_id.in_(ids))
            .order_by(Approval.decided_at)
        )
        decisions = [
            {
                "revision_id": str(a.subject_id),
                "decision": a.decision,
                "acknowledged": list(a.acknowledged),
                "comment": a.comment,
                "decided_at": a.decided_at.isoformat(),
            }
            for a in rows
        ]
    return PlanOut(
        id=plan.id, work_item_id=plan.work_item_id, status=plan.status, current_revision=plan.current_revision,
        base_sha=plan.base_sha, base_branch=plan.base_branch, error=plan.error, auto=plan.auto, revision=rev,
        approvals=decisions, created_at=plan.created_at, updated_at=plan.updated_at,
    )  # fmt: skip


async def owned_plan(db: DB, plan_id: uuid.UUID, user: User, *, lock: bool = False) -> Plan:
    q = select(Plan).where(Plan.id == plan_id, Plan.owner_id == user.id)
    plan = await db.scalar(q.with_for_update() if lock else q)
    if plan is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Plan not found")
    return plan


@router.post("/work-items/{work_item_id}/plans", response_model=PlanOut, status_code=202)
async def start(work_item_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> PlanOut:
    """Start a plan: defers `draft_plan`; poll the plan, or wait for the inbox item."""
    w, *_ = await load(db, work_item_id, auth.user, lock=True)
    plan = await start_plan(db, w)
    await audit(
        db, "plan.start", user_id=auth.user.id, target_type="plan", target_id=str(plan.id),
        detail={"work_item_id": str(w.id)}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    await defer_draft(plan.id, w.project_id)  # type: ignore[arg-type]
    await db.refresh(plan)
    return await plan_out(db, plan)


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: uuid.UUID, auth: CurrentAuth, db: DB) -> PlanOut:
    return await plan_out(db, await owned_plan(db, plan_id, auth.user))
