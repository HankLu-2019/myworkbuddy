"""Merged changes as knowledge (design 08 §F7).

When a job is merged, `index_change()` writes one `change` document: the ticket, the plan's summary and approach,
the files changed, the commit message, what the user asked for in review rounds, and the review request's link. Not
the diff (the code index has the merged code) and not reviewers' or CI text. The document belongs to the job's
owner and has the repository's visibility, so `app/knowledge/acl.py` decides who finds it (rule R2). Indexing again
replaces it (`source_ref = job:<id>`).
"""

import uuid

from sqlalchemy import select

from app.db import get_sessionmaker
from app.enums import JobEventType, JobStatus
from app.knowledge.chunking import ChunkIn, split_text
from app.knowledge.indexer import upsert_document
from app.logs import log
from app.models import JiraIssue, Job, JobEvent, PlanRevision, Project, WorkItem

MAX_SECTION = 4000


def render(key: str, doc: dict, job: Job, project: Project, feedback: list[str]) -> str:
    push = job.push or {}
    link = push.get("mr_url") or push.get("change_url") or ""
    files = (job.result or {}).get("files") or []
    lines = [
        f"# {key}: {doc.get('title', 'Change')} (merged)",
        "",
        f"Repository: {project.name}" + (f" · Review request: {link}" if link else ""),
        "",
        "## Summary",
        "",
        str(doc.get("summary", ""))[:MAX_SECTION],
        "",
        "## Approach",
        "",
        str(doc.get("approach", ""))[:MAX_SECTION],
        "",
        "## Files changed",
        "",
        *[f"- {f['status']} `{f['path']}` (+{f['added']} −{f['removed']})" for f in files[:100]],
    ]
    if push.get("payload"):
        lines += ["", "## Commit message", "", str(push["payload"]["commit"]["message"])[:MAX_SECTION]]
    if feedback:
        lines += ["", "## What the reviewer asked for", ""] + [f"- {f[:1000]}" for f in feedback]
    rounds = job.review_round
    lines += ["", f"Review rounds: {rounds + 1}. Attempts in the last round: {job.attempt}."]
    return "\n".join(lines)


async def index_change(job_id: uuid.UUID) -> int | None:
    """Write (or replace) the job's `change` document. Returns its id, or None when the job isn't merged."""
    async with get_sessionmaker()() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status != JobStatus.merged:
            await db.commit()
            return None
        project = await db.get(Project, job.project_id) if job.project_id else None
        rev = await db.get(PlanRevision, job.plan_revision_id)
        item = await db.get(WorkItem, job.work_item_id)
        issue = await db.get(JiraIssue, item.jira_issue_id) if item else None
        if project is None or rev is None:
            await db.commit()
            return None
        events = await db.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job.id, JobEvent.type == JobEventType.status)
            .order_by(JobEvent.seq)
        )
        feedback = [str(e.data["user_note"]) for e in events if e.data.get("user_note")]
        key = issue.key if issue else "JOB"
        text = render(key, rev.content or {}, job, project, feedback)
        doc = await upsert_document(
            db, owner_id=job.owner_id, source_type="change", source_ref=f"job:{job.id}",
            title=f"{key}: {(rev.content or {}).get('title', 'Change')} (merged)",
            chunks=[ChunkIn(part, {"job_id": str(job.id)}) for part in split_text(text)],
            visibility=project.visibility, project_id=project.id,
            meta={"job_id": str(job.id), "work_item_id": str(job.work_item_id),
                  "url": (job.push or {}).get("mr_url") or (job.push or {}).get("change_url")},
        )  # fmt: skip
        await db.commit()
        log.info("change.indexed", job_id=str(job_id), document_id=doc.id)
        return doc.id
