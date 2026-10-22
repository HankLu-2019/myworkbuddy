"""The outcome dashboard (design 08 §F9): what happened to jobs in a period, and what they cost.

Everything is computed in SQL from `jobs`, `job_events`, `plan_revisions`, `work_items` and `jira_issues`; there are
no new tables. A job belongs to the period it was created in; the period is a range of local dates in the viewer's
time zone (rule R8), and weeks start on the local Monday.

- Outcome: the job's status when it is final (merged, closed, discarded, failed by code, timed out, over budget,
  cancelled), otherwise `in_progress`.
- Attempts: agent runs, counted from status events into `running` (an answered question resumes the same attempt).
- Review rounds: `review_round + 1`. Plan revisions: the number of the revision the job ran.
- Times: start → first `awaiting_review`; first `pushed` → `merged`. Medians over the jobs that got there.
- Cost: `jobs.cost_usd`, plus the plans' revision costs (`plan_revisions.cost_usd`) as planning. Per merged change:
  the merged jobs' cost plus the cost of their plans, over the number merged.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import JobEventType, JobStatus
from app.jobs.state import TERMINAL
from app.models import JiraIssue, Job, JobEvent, Plan, PlanRevision, Project, User, WorkItem

MAX_DAYS = 366
DEFAULT_DAYS = 30
BUCKET_MAX = 5  # distributions fold everything above into "5+"
OUTCOMES = sorted(str(s) for s in TERMINAL)


@dataclass
class Filters:
    start: date
    end: date  # inclusive
    owner_id: int | None  # None: everyone (admins)
    project_id: int | None = None
    issue_type: str | None = None


def zone(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or get_settings().default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def period(user: User, start: date | None, end: date | None, today: date | None = None) -> tuple[date, date]:
    """Defaults to the last 30 local days including today. Raises ValueError for a bad range."""
    today = today or datetime.now(UTC).astimezone(zone(user)).date()
    end = end or today
    start = start or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise ValueError("The start date is after the end date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"Choose a period of at most {MAX_DAYS} days.")
    return start, end


def _bounds(f: Filters, tz: ZoneInfo) -> tuple[datetime, datetime]:
    lo = datetime.combine(f.start, time(0), tz).astimezone(UTC)
    hi = datetime.combine(f.end + timedelta(days=1), time(0), tz).astimezone(UTC)
    return lo, hi


def _week(col, tz_name: str):
    return func.date_trunc("week", func.timezone(tz_name, col))


def _scoped(f: Filters, lo: datetime, hi: datetime):
    q = (
        select(
            Job.id,
            Job.status,
            Job.error_code,
            Job.review_round,
            Job.cost_usd,
            Job.created_at,
            Job.started_at,
            PlanRevision.plan_id,
            PlanRevision.revision.label("plan_revision"),
        )  # fmt: skip
        .join(WorkItem, WorkItem.id == Job.work_item_id)
        .join(PlanRevision, PlanRevision.id == Job.plan_revision_id)
        .outerjoin(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
        .where(Job.created_at >= lo, Job.created_at < hi)
    )
    if f.owner_id is not None:
        q = q.where(Job.owner_id == f.owner_id)
    if f.project_id is not None:
        q = q.where(Job.project_id == f.project_id)
    if f.issue_type is not None:
        q = q.where(JiraIssue.issue_type == f.issue_type)
    return q.cte("scoped")


def _per_job(scoped):
    to = JobEvent.data["to"].astext
    ev = (
        select(
            JobEvent.job_id,
            func.min(JobEvent.at).filter(to == JobStatus.awaiting_review).label("first_review"),
            func.min(JobEvent.at).filter(to == JobStatus.pushed).label("first_push"),
            func.min(JobEvent.at).filter(to == JobStatus.merged).label("merged_at"),
            func.count()
            .filter(to == JobStatus.running, JobEvent.data["from"].astext != JobStatus.needs_input)
            .label("attempts"),
        )  # fmt: skip
        .join(scoped, scoped.c.id == JobEvent.job_id)
        .where(JobEvent.type == JobEventType.status)
        .group_by(JobEvent.job_id)
        .cte("ev")
    )
    outcome = case((scoped.c.status.in_(OUTCOMES), scoped.c.status), else_=literal("in_progress"))
    return (
        select(
            scoped,
            outcome.label("outcome"),
            func.coalesce(ev.c.attempts, 0).label("attempts"),
            ev.c.first_review,
            ev.c.first_push,
            ev.c.merged_at,
        )  # fmt: skip
        .outerjoin(ev, ev.c.job_id == scoped.c.id)
        .cte("per_job")
    )


def _minutes(later, earlier):
    return func.extract("epoch", later - earlier) / 60.0


def _median(expr):
    """NULLs (jobs that didn't get there) are left out by the aggregate itself."""
    return func.percentile_cont(0.5).within_group(expr)


def _f(value: Any, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


async def _distribution(db: AsyncSession, per_job, col) -> list[dict[str, Any]]:
    bucket = func.least(col, BUCKET_MAX)
    rows = await db.execute(select(bucket, func.count()).select_from(per_job).group_by(bucket).order_by(bucket))
    return [{"value": int(v), "label": f"{BUCKET_MAX}+" if v >= BUCKET_MAX else str(int(v)), "jobs": n}
            for v, n in rows]  # fmt: skip


def _plan_filter(q, f: Filters, lo: datetime, hi: datetime):
    q = (
        q.join(Plan, Plan.id == PlanRevision.plan_id)
        .join(WorkItem, WorkItem.id == Plan.work_item_id)
        .outerjoin(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
        .where(PlanRevision.created_at >= lo, PlanRevision.created_at < hi)
    )
    if f.owner_id is not None:
        q = q.where(Plan.owner_id == f.owner_id)
    if f.project_id is not None:
        q = q.where(WorkItem.project_id == f.project_id)
    if f.issue_type is not None:
        q = q.where(JiraIssue.issue_type == f.issue_type)
    return q


def _weeks(start: date, end: date) -> list[date]:
    first = start - timedelta(days=start.weekday())
    out = []
    while first <= end:
        out.append(first)
        first += timedelta(days=7)
    return out


async def insights(db: AsyncSession, viewer: User, f: Filters) -> dict[str, Any]:
    tz = zone(viewer)
    lo, hi = _bounds(f, tz)
    per_job = _per_job(_scoped(f, lo, hi))
    c = per_job.c
    merged = c.outcome == JobStatus.merged

    totals = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(merged),
                func.coalesce(func.sum(c.cost_usd), 0),
                func.coalesce(func.sum(c.cost_usd).filter(merged), 0),
                _median(_minutes(c.first_review, c.started_at)),
                _median(_minutes(c.merged_at, c.first_push)),
                func.avg(c.attempts),
                func.avg(c.review_round + 1),
                func.avg(c.plan_revision),
            ).select_from(per_job)
        )
    ).one()
    jobs, n_merged, job_cost, merged_job_cost, to_review, to_merge, avg_attempts, avg_rounds, avg_revisions = totals

    code = case((c.outcome == JobStatus.failed, func.coalesce(c.error_code, "unknown")), else_=None)
    outcomes = [
        {"outcome": o, "error_code": err, "jobs": n, "cost_usd": _f(cost)}
        for o, err, n, cost in await db.execute(
            select(c.outcome, code, func.count(), func.coalesce(func.sum(c.cost_usd), 0))
            .select_from(per_job)
            .group_by(c.outcome, code)
            .order_by(func.count().desc(), c.outcome, code)
        )
    ]

    merged_plans = select(c.plan_id).select_from(per_job).where(merged).distinct().scalar_subquery()
    merged_plan_cost = await db.scalar(
        select(func.coalesce(func.sum(PlanRevision.cost_usd), 0)).where(PlanRevision.plan_id.in_(merged_plans))
    )
    planning = await db.scalar(_plan_filter(select(func.coalesce(func.sum(PlanRevision.cost_usd), 0)), f, lo, hi))

    tz_name = tz.key
    wk = _week(c.created_at, tz_name)
    by_week: dict[date, dict[str, Any]] = {
        w: {"week_start": w, "jobs": 0, "merged": 0, "job_cost_usd": 0.0, "planning_cost_usd": 0.0}
        for w in _weeks(f.start, f.end)
    }
    for w, n, m, cost in await db.execute(
        select(wk, func.count(), func.count().filter(merged), func.coalesce(func.sum(c.cost_usd), 0))
        .select_from(per_job).group_by(wk)
    ):  # fmt: skip
        by_week[w.date()] |= {"jobs": n, "merged": m, "job_cost_usd": _f(cost)}
    pw = _week(PlanRevision.created_at, tz_name)
    for w, cost in await db.execute(
        _plan_filter(select(pw, func.coalesce(func.sum(PlanRevision.cost_usd), 0)), f, lo, hi).group_by(pw)
    ):
        by_week[w.date()]["planning_cost_usd"] = _f(cost)
    weeks = [w | {"cost_usd": round(w["job_cost_usd"] + w["planning_cost_usd"], 4)} for w in by_week.values()]

    per_merged = (float(merged_job_cost) + float(merged_plan_cost or 0)) / n_merged if n_merged else None
    return {
        "start": f.start, "end": f.end, "timezone": tz_name, "scope": "me" if f.owner_id is not None else "all",
        "jobs": jobs, "merged": n_merged, "outcomes": outcomes,
        "median_minutes_to_review": _f(to_review, 1), "median_minutes_push_to_merge": _f(to_merge, 1),
        "attempts": {"mean": _f(avg_attempts, 2), "distribution": await _distribution(db, per_job, c.attempts)},
        "review_rounds": {"mean": _f(avg_rounds, 2),
                          "distribution": await _distribution(db, per_job, c.review_round + 1)},
        "plan_revisions": {"mean": _f(avg_revisions, 2),
                           "distribution": await _distribution(db, per_job, c.plan_revision)},
        "cost": {"jobs_usd": _f(job_cost), "planning_usd": _f(planning or 0),
                 "total_usd": _f(float(job_cost) + float(planning or 0)), "per_merged_usd": _f(per_merged)},
        "weeks": weeks,
    }  # fmt: skip


async def options(db: AsyncSession, owner_id: int | None) -> dict[str, Any]:
    """The repositories and issue types that have jobs in scope, for the filters."""
    q = select(Project.id, Project.name).join(Job, Job.project_id == Project.id).distinct().order_by(Project.name)
    t = (
        select(JiraIssue.issue_type)
        .join(WorkItem, WorkItem.jira_issue_id == JiraIssue.id)
        .join(Job, Job.work_item_id == WorkItem.id)
        .where(JiraIssue.issue_type.is_not(None))
        .distinct()
        .order_by(JiraIssue.issue_type)
    )
    if owner_id is not None:
        q, t = q.where(Job.owner_id == owner_id), t.where(Job.owner_id == owner_id)
    return {
        "projects": [{"id": i, "name": n} for i, n in await db.execute(q)],
        "issue_types": list(await db.scalars(t)),
    }
