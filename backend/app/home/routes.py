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
from app.knowledge.acl import visible_filter
from app.llm import keys
from app.logs import log
from app.models import ChatSession, LibrarianRun, Message, Project, User, UserSecret, WikiPage
from app.secrets.service import SECRET_KINDS, readable

router = APIRouter(prefix="/api", tags=["home"])

BUDGET_WARN_RATIO = 0.8
LIBRARIAN_RECENT = timedelta(hours=36)  # "last night", wherever the user's night is
TOKEN_KINDS = {"gitlab_token", "gerrit_password", "jira_token"}


class NeedsYou(BaseModel):
    """One card. The same message and fix button wherever the problem shows up (07-ux §5.10)."""

    kind: Literal["ingest_failed", "budget_warning", "budget_exceeded", "credential_unreadable"]
    key: str  # stable id, so the client can tell cards apart
    message: str
    action: str
    href: str
    at: datetime | None = None


class ActivityItem(BaseModel):
    kind: Literal["ingest"]
    id: int
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


class HomeOut(BaseModel):
    needs_you: list[NeedsYou]
    running: list[ActivityItem]
    recent_chats: list[RecentChat]
    recent_pages: list[RecentPage]
    librarian: LibrarianSummary | None
    checklists: list[Checklist]


class ActivityOut(BaseModel):
    items: list[ActivityItem]


async def _running(db: DB, user: User) -> list[ActivityItem]:
    rows = await db.scalars(
        select(Project)
        .where(Project.owner_id == user.id, Project.status.in_(["queued", "running"]))
        .order_by(Project.updated_at)
    )
    return [
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


@router.get("/activity", response_model=ActivityOut)
async def activity(auth: CurrentAuth, db: DB) -> ActivityOut:
    """Background work of the caller that is still going. Phase 2+ adds runs, plan drafts and jobs."""
    return ActivityOut(items=await _running(db, auth.user))


async def _member_steps(db: DB, user: User) -> list[ChecklistStep]:
    own_sessions = select(ChatSession.id).where(ChatSession.owner_id == user.id)
    has_token = await db.scalar(select(exists().where(UserSecret.user_id == user.id, UserSecret.kind.in_(TOKEN_KINDS))))
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
            label="Add your GitLab, Gerrit or Jira token",
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
    for s in await db.scalars(select(UserSecret).where(UserSecret.user_id == user.id)):
        if not readable(s.ciphertext):
            label = SECRET_KINDS.get(s.kind, s.kind)
            needs.append(
                NeedsYou(
                    kind="credential_unreadable",
                    key=f"secret:{s.kind}",
                    message=f"Your {label} can't be read any more. Enter it again.",
                    action="Update token",
                    href=f"/settings/integrations?focus={s.kind}",
                    at=s.updated_at,
                )
            )
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
        running=running,
        recent_chats=chats,
        recent_pages=pages,
        librarian=librarian,
        checklists=checklists,
    )
