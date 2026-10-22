# ruff: noqa: F811 - pytest fixtures imported from the job tests
"""Merged changes as knowledge (design 08 §F7; plan F7)."""

import uuid
from types import SimpleNamespace

from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.jobs import changes, pipeline, track
from app.knowledge.chunking import ChunkIn
from app.knowledge.indexer import upsert_document
from app.knowledge.search import hybrid_search
from app.models import Document, Project, User
from app.plans import planner
from tests.test_job_pipeline import approved_ticket, forge_url, job_row, runner, start  # noqa: F401 - fixtures
from tests.test_job_push import approve, forge, reviewed  # noqa: F401 - fixtures


async def merged(make_user, client_for, forge_url, forge, task_queue, *, feedback: str | None = None) -> tuple:
    t, job_id = await reviewed(make_user, client_for, forge_url)
    c = await client_for("alice", create=False)
    assert (await approve(c, job_id)).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    if feedback:
        assert (await c.post(f"/api/jobs/{job_id}/request-changes", json={"feedback": feedback})).status_code == 200
        assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
        assert (await approve(c, job_id)).status_code == 200
        assert await pipeline.advance(uuid.UUID(job_id)) == "pushed"
    await forge.post("/fake/merge/1")
    assert (await track.track_pushed())["merged"] == 1
    assert [j for j in task_queue.jobs.values() if j["task_name"] == "index_change"]
    return t, job_id


async def docs() -> list[Document]:
    async with get_sessionmaker()() as db:
        return list(await db.scalars(select(Document).where(Document.source_type == "change")))


async def test_a_merged_job_becomes_one_change_document(
    make_user, client_for, forge_url, forge, runner, task_queue
) -> None:
    t, job_id = await merged(make_user, client_for, forge_url, forge, task_queue, feedback="Use exponential backoff.")
    doc_id = await changes.index_change(uuid.UUID(job_id))
    assert doc_id and await changes.index_change(uuid.UUID(job_id)) == doc_id  # idempotent
    [doc] = await docs()
    assert doc.owner_id == t["user"].id and doc.visibility == "private" and doc.project_id == t["project"].id
    assert doc.title == "PAY-12: Retry webhook delivery (merged)" and doc.source_ref == f"job:{job_id}"
    async with get_sessionmaker()() as db:
        alice = await db.get(User, t["user"].id)
        hits = await hybrid_search(db, alice, "retry webhook delivery merged", source_types=["change"])
        await db.commit()
    text = "\n".join(h.content for h in hits)
    assert "## Files changed" in text and "`app/webhooks.py`" in text and "Retry on 5xx." in text
    assert "## What the reviewer asked for\n\n- Use exponential backoff." in text
    assert "PAY-12 Retry webhook delivery" in text and "/merge_requests/1" in text  # commit message, MR link
    assert await changes.index_change(uuid.UUID(int=1)) is None


async def test_another_users_private_change_never_shows(
    make_user, client_for, forge_url, forge, runner, task_queue
) -> None:
    t, job_id = await merged(make_user, client_for, forge_url, forge, task_queue)
    await changes.index_change(uuid.UUID(job_id))
    bob = await make_user("bob")
    async with get_sessionmaker()() as db:
        hits = await hybrid_search(db, bob, "retry webhook delivery merged", source_types=["change"])
        await db.commit()
    assert hits == []
    c = await client_for("bob", create=False)
    r = await c.get("/api/knowledge/search", params={"q": "retry webhook delivery merged"})
    body = r.json()
    items = body if isinstance(body, list) else body.get("items", body.get("results", []))
    assert r.status_code == 200 and all(h.get("source_type") != "change" for h in items)
    # A shared repository shares its changes.
    async with get_sessionmaker()() as db:
        await db.execute(update(Project).where(Project.id == t["project"].id).values(visibility="public"))
        await db.commit()
    await changes.index_change(uuid.UUID(job_id))
    async with get_sessionmaker()() as db:
        hits = await hybrid_search(db, bob, "retry webhook delivery merged", source_types=["change"])
        await db.commit()
    assert [h.title for h in hits][:1] == ["PAY-12: Retry webhook delivery (merged)"]


async def test_the_planner_sees_similar_changes_of_the_same_repository_only(
    make_user, client_for, forge_url, forge, runner, task_queue
) -> None:
    t, job_id = await merged(make_user, client_for, forge_url, forge, task_queue)
    await changes.index_change(uuid.UUID(job_id))
    async with get_sessionmaker()() as db:
        other = Project(owner_id=t["user"].id, visibility="private", name="ledger", host="gitlab", status="ready",
                        repo_url="https://g/team/ledger.git")  # fmt: skip
        db.add(other)
        await db.flush()
        await upsert_document(db, owner_id=t["user"].id, source_type="change", source_ref="job:other",
                              title="LED-1: Retry webhook delivery in the ledger (merged)",
                              chunks=[ChunkIn("Retry webhook delivery on 5xx in the ledger.")],
                              visibility="private", project_id=other.id)  # fmt: skip
        await db.commit()
        alice = await db.get(User, t["user"].id)
        db.expunge(alice)
    issue = {"summary": "Retry webhook delivery", "description": "Webhooks are dropped on 503."}
    inputs = SimpleNamespace(owner=alice, project=SimpleNamespace(id=t["project"].id), issue=issue)
    similar = await planner.similar_changes(inputs)  # type: ignore[arg-type]
    assert [h.title for h in similar] == ["PAY-12: Retry webhook delivery (merged)"]
    prompt = planner.prompt_for(
        SimpleNamespace(issue_key="PAY-13", project=SimpleNamespace(name="payments"), issue={}, card="", how_i_work="",
                        previous=None),  # type: ignore[arg-type]
        "main", "a" * 40, None, similar=similar,
    )  # fmt: skip
    assert "## Similar past changes in this repository" in prompt
    assert (
        f'"document_id": {similar[0].document_id}, "source": "change:{similar[0].document_id}", "untrusted": true'
        in prompt
    )
