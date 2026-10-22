"""Home and the activity indicator (design 07-ux §4.1, §5.1, §5.10, §6.3).

`GET /api/home` returns every section in one call; `GET /api/activity` is the light poll behind the top bar.
Database reads happen first and the transaction is closed before the gateway is asked for spend (rule R1).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import exists, func, select

from app.deps import DB, CurrentAuth
from app.integrations.service import credential_label, state_of
from app.knowledge.acl import visible_filter
from app.llm import keys
from app.logs import log
from app.models import (
    Agent,
    ChatSession,
    InboxItem,
    Integration,
    JiraIssue,
    Job,
    LibrarianRun,
    Message,
    Plan,
    Project,
    Run,
    Schedule,
    User,
    UserCredential,
    WikiPage,
    WorkItem,
)

router = APIRouter(prefix="/api", tags=["home"])

BUDGET_WARN_RATIO = 0.8
LIBRARIAN_RECENT = timedelta(hours=36)  # "last night", wherever the user's night is


class NeedsYou(BaseModel):
    """One card. The same message and fix button wherever the problem shows up (07-ux §5.10)."""

    kind: Literal[
        "ingest_failed",
        "budget_warning",
        "budget_exceeded",
        "credential_invalid",
        "credential_unreadable",
        "alert",
        "schedule_paused",
        "plan_ready",
        "job_review",
    ]
    key: str  # stable id, so the client can tell cards apart
    message: str
    action: str
    href: str
    at: datetime | None = None


class ActivityItem(BaseModel):
    kind: Literal["ingest", "run", "plan", "job"]
    id: int | str  # project id, run id or plan id
    title: str
    status: Literal["queued", "running"]
    step: str | None
    since: datetime
    href: str


class RecentChat(BaseModel):
    id: str
    title: str
    updated_at: datetime


class RecentPage(BaseModel):
    id: int
    title: str
    path: str
    visibility: Literal["private", "public"]
    mine: bool
    updated_at: datetime


class LibrarianSummary(BaseModel):
    pages_changed: int
    rejected: int
    at: datetime


class ChecklistStep(BaseModel):
    key: str
    label: str
    href: str
    done: bool
    optional: bool = False


class Checklist(BaseModel):
    kind: Literal["getting_started", "admin_setup"]
    steps: list[ChecklistStep]


class TodayBrief(BaseModel):
    """The latest morning brief from the last 24 hours (07-ux §4.1 "Today")."""

    run_id: str
    schedule_name: str
    headline: str
    status: Literal["ok", "warning", "alert"]
    at: datetime


class HomeOut(BaseModel):
    needs_you: list[NeedsYou]
    today: TodayBrief | None = None
    running: list[ActivityItem]
    recent_chats: list[RecentChat]
    recent_pages: list[RecentPage]
    librarian: LibrarianSummary | None
    checklists: list[Checklist]
    board_needs: int = 0  # the Board badge (07 §3.1): plans waiting for a decision + jobs ready for review
    jobs_running: int = 0  # the Jobs badge (07 §3.1)


class ActivityOut(BaseModel):
    items: list[ActivityItem]


async def _running(db: DB, user: User) -> list[ActivityItem]:
    rows = await db.scalars(
        select(Project)
        .where(Project.owner_id == user.id, Project.status.in_(["queued", "running"]))
        .order_by(Project.updated_at)
    )
    items = [
        ActivityItem(
            kind="ingest",
            id=p.id,
            title=p.name,
            status=p.status,
            step=p.progress if p.status == "running" else "Waiting to start",
            since=p.updated_at,
            href=f"/sources/repos/{p.id}",
        )
        for p in rows
    ]
    runs = await db.scalars(
        select(Run)
        .where(Run.owner_id == user.id, Run.status.in_(["queued", "running"]))
        .order_by(Run.created_at)
        .limit(20)
    )
    for r in runs:
        items.append(
            ActivityItem(
                kind="run",
                id=str(r.id),
                title=r.config.get("name", "Run"),
                status=r.status,
                step="Running checks" if r.status == "running" else "Waiting to start",
                since=r.started_at or r.created_at,
                href=f"/runs/{r.id}",
            )  # fmt: skip
        )
    drafting = await db.execute(
        select(Plan, JiraIssue.key)
        .join(WorkItem, WorkItem.id == Plan.work_item_id)
        .join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
        .where(Plan.owner_id == user.id, Plan.status == "drafting")
        .order_by(Plan.updated_at)
        .limit(20)
    )
    for plan, key in drafting.all():
        items.append(
            ActivityItem(
                kind="plan",
                id=str(plan.id),
                title=f"Plan for {key}",
                status="running",
                step="Revising the plan" if plan.current_revision else "Drafting the plan",
                since=plan.updated_at,
                href=f"/work/{plan.work_item_id}/plan",
            )  # fmt: skip
        )
    steps = {"created": "Waiting to start", "preparing": "Preparing the workspace", "running": "The agent is working",
             "verifying": "Checking the result", "approved_push": "Waiting to push", "pushing": "Pushing"}  # fmt: skip
    jobs = await db.execute(
        select(Job, JiraIssue.key)
        .join(WorkItem, WorkItem.id == Job.work_item_id)
        .join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
        .where(Job.owner_id == user.id, Job.status.in_(list(steps)))
        .order_by(Job.created_at)
        .limit(20)
    )
    for job, key in jobs.all():
        step = steps[job.status]
        if job.status == "running":
            step += f" (attempt {job.attempt} of {job.policy.get('max_attempts', 3)})"
        items.append(
            ActivityItem(
                kind="job",
                id=str(job.id),
                title=f"Job for {key}",
                status="queued" if job.status == "created" else "running",
                step=step,
                since=job.started_at or job.created_at,
                href=f"/work/{job.work_item_id}/job",
            )  # fmt: skip
        )
    return items


@router.get("/activity", response_model=ActivityOut)
async def activity(auth: CurrentAuth, db: DB) -> ActivityOut:
    """Background work of the caller that is still going: ingests and runs (Phase 3+ adds plan drafts, jobs)."""
    return ActivityOut(items=await _running(db, auth.user))


async def _member_steps(db: DB, user: User) -> list[ChecklistStep]:
    own_sessions = select(ChatSession.id).where(ChatSession.owner_id == user.id)
    has_token = await db.scalar(
        select(exists().where(UserCredential.user_id == user.id, UserCredential.status == "ok"))
    )
    has_ready_repo = await db.scalar(select(exists().where(Project.owner_id == user.id, Project.status == "ready")))
    has_import = await db.scalar(
        select(exists().where(ChatSession.owner_id == user.id, ChatSession.source == "claude-code"))
    )
    has_question = await db.scalar(select(exists().where(Message.session_id.in_(own_sessions), Message.role == "user")))
    return [
        ChecklistStep(
            key="timezone", label="Confirm your timezone", href="/settings/profile", done=bool(user.timezone_confirmed)
        ),
        ChecklistStep(
            key="tokens",
            label="Connect GitLab, Gerrit or Jira and test it",
            href="/settings/integrations",
            done=bool(has_token),
        ),
        ChecklistStep(
            key="repository", label="Add a repository", href="/sources/repos?add=1", done=bool(has_ready_repo)
        ),
        ChecklistStep(
            key="import",
            label="Import Claude Code sessions",
            href="/sources/imports",
            done=bool(has_import),
            optional=True,
        ),
        ChecklistStep(key="question", label="Ask your first question", href="/chat?new=1", done=bool(has_question)),
    ]


async def _admin_steps(db: DB, ready: bool) -> list[ChecklistStep]:
    users = await db.scalar(select(func.count()).select_from(User).where(User.is_active)) or 0
    budgets = await db.scalar(select(exists().where(User.llm_budget_usd.is_not(None))))
    return [
        ChecklistStep(key="system", label="Check that System is green", href="/admin/system", done=ready),
        ChecklistStep(key="users", label="Add users", href="/admin", done=users > 1),
        ChecklistStep(key="budgets", label="Set AI budgets", href="/admin", done=bool(budgets), optional=True),
    ]


def _budget_card(user: User, info: dict | None) -> NeedsYou | None:
    budget = float(keys.budget_for(user))
    if not info or budget <= 0:
        return None
    ratio = info["spend_usd"] / budget
    if ratio >= 1:
        return NeedsYou(
            kind="budget_exceeded",
            key="budget",
            message=f"You've reached this month's AI budget (${budget:.2f}). An admin can raise it.",
            action="View usage",
            href="/settings/usage",
        )
    if ratio >= BUDGET_WARN_RATIO:
        return NeedsYou(
            kind="budget_warning",
            key="budget",
            message=f"You've used {ratio:.0%} of this month's AI budget (${budget:.2f}).",
            action="View usage",
            href="/settings/usage",
        )
    return None


@router.get("/home", response_model=HomeOut)
async def home(auth: CurrentAuth, db: DB) -> HomeOut:
    user = auth.user
    needs: list[NeedsYou] = []
    failed = await db.scalars(
        select(Project)
        .where(Project.owner_id == user.id, Project.status == "failed")
        .order_by(Project.updated_at.desc())
    )
    for p in failed:
        reason = (p.error or "unknown error").splitlines()[0][:200]
        needs.append(
            NeedsYou(
                kind="ingest_failed",
                key=f"ingest:{p.id}",
                message=f"Indexing {p.name} failed: {reason}",
                action="Open repository",
                href=f"/sources/repos/{p.id}",
                at=p.updated_at,
            )
        )
    creds = await db.execute(
        select(UserCredential, Integration)
        .join(Integration, Integration.id == UserCredential.integration_id)
        .where(UserCredential.user_id == user.id, Integration.enabled)
        .order_by(Integration.name, UserCredential.kind)
    )
    seen: set[int] = set()
    for cred, integration in creds.all():
        if integration.id in seen:
            continue  # one card per integration, even when several parts are bad
        focus = f"/settings/integrations?focus={integration.id}"
        label = credential_label(integration, cred.kind)
        if state_of(cred) == "unreadable":
            seen.add(integration.id)
            needs.append(
                NeedsYou(
                    kind="credential_unreadable",
                    key=f"credential:{integration.id}",
                    message=f"Your {label} can't be read any more. Enter it again.",
                    action="Update token",
                    href=focus,
                    at=cred.updated_at,
                )  # fmt: skip
            )
        elif cred.status == "invalid":
            seen.add(integration.id)
            reason = f" ({cred.last_error})" if cred.last_error else ""
            needs.append(
                NeedsYou(
                    kind="credential_invalid",
                    key=f"credential:{integration.id}",
                    message=(
                        f"Your {label} was rejected{reason}. "
                        f"Anything that needs {integration.name} fails until you update it."
                    ),
                    action="Update token",
                    href=focus,
                    at=cred.updated_at,
                )  # fmt: skip
            )
    alerts = await db.scalars(
        select(InboxItem)
        .where(InboxItem.owner_id == user.id, InboxItem.read_at.is_(None), InboxItem.kind == "alert")
        .order_by(InboxItem.created_at.desc())
        .limit(5)
    )
    for item in alerts:
        needs.append(
            NeedsYou(
                kind="alert",
                key=f"inbox:{item.id}",
                message=item.title,
                action="Open alert",
                href=item.link or "/inbox",
                at=item.created_at,
            )  # fmt: skip
        )
    decide = (
        await db.execute(
            select(WorkItem.id, JiraIssue.key, WorkItem.updated_at)
            .join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
            .where(WorkItem.owner_id == user.id, WorkItem.state == "needs_decision")
            .order_by(WorkItem.updated_at.desc())
        )
    ).all()
    review = (
        await db.execute(
            select(Job.work_item_id, JiraIssue.key, Job.updated_at)
            .join(WorkItem, WorkItem.id == Job.work_item_id)
            .join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
            .where(Job.owner_id == user.id, Job.status == "awaiting_review")
            .order_by(Job.updated_at.desc())
        )
    ).all()
    for item_id, key, at in review[:5]:
        needs.append(
            NeedsYou(
                kind="job_review",
                key=f"job:{item_id}",
                message=f"The job for {key} is ready for review",
                action="Review changes",
                href=f"/work/{item_id}/job",
                at=at,
            )  # fmt: skip
        )
    for item_id, key, at in decide[:5]:
        needs.append(
            NeedsYou(
                kind="plan_ready",
                key=f"work:{item_id}",
                message=f"The plan for {key} needs a decision",
                action="Review plan",
                href=f"/work/{item_id}/plan",
                at=at,
            )  # fmt: skip
        )
    paused = await db.scalars(
        select(Schedule)
        .where(Schedule.owner_id == user.id, Schedule.enabled.is_(False), Schedule.paused_reason.is_not(None))
        .order_by(Schedule.updated_at.desc())
        .limit(10)
    )
    for sch in paused:
        reason = sch.paused_reason or ""
        needs.append(
            NeedsYou(
                kind="schedule_paused",
                key=f"schedule:{sch.id}",
                message=reason,
                action="Review",
                href=f"/schedules/{sch.id}",
                at=sch.last_run_at or sch.updated_at,
            )  # fmt: skip
        )
    brief = await db.execute(
        select(Run, Agent.slug)
        .join(Agent, Agent.id == Run.agent_id)
        .where(
            Run.owner_id == user.id,
            Run.status == "succeeded",
            Agent.slug == "morning-brief",
            Run.finished_at >= datetime.now(UTC) - timedelta(hours=24),
        )  # fmt: skip
        .order_by(Run.finished_at.desc())
        .limit(1)
    )
    today = None
    if row := brief.first():
        run = row[0]
        report = run.report or {}
        today = TodayBrief(
            run_id=str(run.id), schedule_name=run.config.get("name", "Morning brief"),
            headline=report.get("headline", ""), status=report.get("status", "ok"), at=run.finished_at,
        )  # fmt: skip
    running = await _running(db, user)
    chats = [
        RecentChat(id=str(c.id), title=c.title, updated_at=c.updated_at)
        for c in await db.scalars(
            select(ChatSession)
            .where(ChatSession.owner_id == user.id, ChatSession.source == "chat")
            .order_by(ChatSession.updated_at.desc())
            .limit(5)
        )
    ]
    pages = [
        RecentPage(
            id=p.id,
            title=p.title,
            path=p.path,
            visibility=p.visibility,
            mine=p.owner_id == user.id,
            updated_at=p.updated_at,
        )  # fmt: skip
        for p in await db.scalars(
            select(WikiPage)
            .where(visible_filter(WikiPage, user), WikiPage.status == "ok")
            .order_by(WikiPage.updated_at.desc())
            .limit(5)
        )
    ]
    since = datetime.now(UTC) - LIBRARIAN_RECENT
    runs = list(
        await db.scalars(select(LibrarianRun).where(LibrarianRun.user_id == user.id, LibrarianRun.started_at >= since))
    )
    librarian = None
    if runs and any(r.pages_updated or r.pages_rejected for r in runs):
        librarian = LibrarianSummary(
            pages_changed=len({p for r in runs for p in r.pages_updated}),
            rejected=sum(len(r.pages_rejected) for r in runs),
            at=max(r.started_at for r in runs),
        )
    member_steps = await _member_steps(db, user)
    admin_steps = await _admin_steps(db, ready=False) if user.is_admin else None
    await db.commit()  # rule R1: spend and readiness below are network calls

    async def spend() -> dict | None:
        try:
            return await keys.key_info(user.id)
        except Exception as exc:  # spend is optional on Home
            log.warning("home.spend_unavailable", user_id=user.id, error=str(exc)[:200])
            return None

    async def system_ok() -> bool:
        if not user.is_admin:
            return False
        from app.ops.health import readiness

        return all(v == "ok" for v in (await readiness()).values())

    info, ready = await asyncio.gather(spend(), system_ok())
    if card := _budget_card(user, info):
        needs.insert(0, card)
    checklists = [Checklist(kind="getting_started", steps=member_steps)]
    if admin_steps is not None:
        admin_steps[0].done = ready
        checklists.insert(0, Checklist(kind="admin_setup", steps=admin_steps))
    return HomeOut(
        needs_you=needs,
        today=today,
        running=running,
        recent_chats=chats,
        recent_pages=pages,
        librarian=librarian,
        checklists=checklists,
        board_needs=len(decide) + len(review),
        jobs_running=sum(1 for r in running if r.kind == "job"),
    )
