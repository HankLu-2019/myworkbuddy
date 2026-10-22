"""Insights API (design 08 §F9): members read their own jobs; admins may read everyone's (`scope=all`)."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from app.deps import DB, CurrentAuth
from app.errors import AppError
from app.insights import service

router = APIRouter(prefix="/api/insights", tags=["insights"])

Outcome = Literal[
    "merged", "closed", "discarded", "failed", "timed_out", "budget_exceeded", "cancelled", "in_progress"
]  # fmt: skip


class OutcomeRow(BaseModel):
    outcome: Outcome
    error_code: str | None
    jobs: int
    cost_usd: float


class Bucket(BaseModel):
    value: int
    label: str
    jobs: int


class Distribution(BaseModel):
    mean: float | None
    distribution: list[Bucket]


class Cost(BaseModel):
    jobs_usd: float
    planning_usd: float
    total_usd: float
    per_merged_usd: float | None


class Week(BaseModel):
    week_start: date
    jobs: int
    merged: int
    job_cost_usd: float
    planning_cost_usd: float
    cost_usd: float


class InsightsOut(BaseModel):
    start: date
    end: date
    timezone: str
    scope: Literal["me", "all"]
    jobs: int
    merged: int
    outcomes: list[OutcomeRow]
    median_minutes_to_review: float | None
    median_minutes_push_to_merge: float | None
    attempts: Distribution
    review_rounds: Distribution
    plan_revisions: Distribution
    cost: Cost
    weeks: list[Week]


class ProjectOption(BaseModel):
    id: int
    name: str


class InsightsOptions(BaseModel):
    projects: list[ProjectOption]
    issue_types: list[str]


def _owner(auth, scope: str) -> int | None:
    if scope == "all":
        if not auth.user.is_admin:
            raise AppError(status.HTTP_403_FORBIDDEN, "forbidden", "Only admins can see everyone's jobs.")
        return None
    return auth.user.id


@router.get("", response_model=InsightsOut)
async def get_insights(
    auth: CurrentAuth,
    db: DB,
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    scope: Literal["me", "all"] = "me",
    project_id: int | None = None,
    issue_type: str | None = Query(None, max_length=60),
) -> InsightsOut:
    owner_id = _owner(auth, scope)
    try:
        start, end = service.period(auth.user, start, end)
    except ValueError as exc:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", str(exc), field="from") from None
    f = service.Filters(start=start, end=end, owner_id=owner_id, project_id=project_id, issue_type=issue_type or None)
    return InsightsOut.model_validate(await service.insights(db, auth.user, f))


@router.get("/options", response_model=InsightsOptions)
async def get_options(auth: CurrentAuth, db: DB, scope: Literal["me", "all"] = "me") -> InsightsOptions:
    return InsightsOptions.model_validate(await service.options(db, _owner(auth, scope)))
