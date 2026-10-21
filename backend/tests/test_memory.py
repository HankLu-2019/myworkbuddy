from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.db import get_sessionmaker
from app.knowledge.episodes import build_episode, sessions_needing_episodes
from app.knowledge.librarian import update_user_wiki
from app.knowledge.wiki import LIBRARIAN, find_page, read_page, save_page
from app.llm.models import set_model_override
from app.models import ChatSession, Episode, Message, Project, User
from app.tasks.app import episode_sweep
from tests.helpers import scripted_model

SUMMARY = {
    "title": "Fix Spark OOM in the metrics pipeline",
    "goal": "Stop executor OOMKilled failures",
    "decisions": ["Raise spark.executor.memory to 8g"],
    "repos": ["pipeline"],
    "files": ["jobs/aggregate.py"],
    "tickets": ["SPIN-4412"],
    "outcome": "Job passes",
    "open_items": [],
    "learnings": ["Prefers fixing config before code"],
}


async def _session_with_messages(owner: User, n: int = 2, idle: bool = True) -> ChatSession:
    async with get_sessionmaker()() as db:
        s = ChatSession(owner_id=owner.id, title="spark oom")
        db.add(s)
        await db.flush()
        for i in range(n):
            db.add(Message(session_id=s.id, role="user" if i % 2 == 0 else "assistant", content=f"msg {i}"))
        if idle:
            s.updated_at = datetime.now(UTC) - timedelta(hours=2)
        await db.commit()
        return s


async def test_build_episode_and_search(make_user, client_for):
    alice = await client_for("alice")
    owner = User(id=alice.user_id, username="alice")
    s = await _session_with_messages(owner)
    set_model_override(scripted_model(structured=SUMMARY))
    async with get_sessionmaker()() as db:
        episode = await build_episode(db, s.id)
        await db.commit()
        assert episode.summary["tickets"] == ["SPIN-4412"] and episode.message_count == 2
        # Same message count: no new LLM call, same episode returned.
        set_model_override(scripted_model(structured={**SUMMARY, "title": "changed"}))
        again = await build_episode(db, s.id)
        assert again.summary["title"] == SUMMARY["title"]

    hits = (await alice.get("/api/knowledge/search", params={"q": "SPIN-4412", "scope": "sessions"})).json()
    assert hits[0]["title"] == SUMMARY["title"]
    assert hits[0]["source_ref"] == f"session:{s.id}"

    # Sharing the session shares its summary.
    bob = await client_for("bob")
    assert (await bob.get("/api/knowledge/search", params={"q": "SPIN-4412"})).json() == []
    await alice.patch(f"/api/chat/sessions/{s.id}", json={"visibility": "public"})
    assert (await bob.get("/api/knowledge/search", params={"q": "SPIN-4412"})).json()[0]["visibility"] == "public"


async def test_sweep_finds_idle_and_grown_sessions(make_user, task_queue):
    alice = await make_user("alice")
    idle = await _session_with_messages(alice)
    await _session_with_messages(alice, idle=False)
    async with get_sessionmaker()() as db:
        assert await sessions_needing_episodes(db) == [idle.id]
        db.add(Episode(session_id=idle.id, owner_id=alice.id, summary=SUMMARY, message_count=2))
        await db.commit()
        assert await sessions_needing_episodes(db) == []
        db.add(Message(session_id=idle.id, role="user", content="one more"))
        await db.commit()
        assert await sessions_needing_episodes(db) == [idle.id]

    await episode_sweep(timestamp=0)
    assert [j["args"] for j in task_queue.jobs.values()] == [{"session_id": str(idle.id)}]


async def test_librarian_updates_private_pages_only(make_user, task_queue):
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        card = await save_page(
            db, owner=alice, path="projects/DataPipeline.md", content="# DataPipeline\n\nOld card.", author=LIBRARIAN
        )
        db.add(
            Project(
                owner_id=alice.id, name="DataPipeline", repo_url="https://x/dp.git", host="gitlab", card_page_id=card.id
            )
        )
        s = ChatSession(owner_id=alice.id, title="x")
        db.add(s)
        await db.flush()
        db.add(
            Episode(
                session_id=s.id,
                owner_id=alice.id,
                message_count=2,
                summary={**SUMMARY, "repos": ["data-pipeline", "unknown-repo"]},
            )
        )
        await db.commit()

    def rewrite(prompt: str) -> str:
        path = prompt.split("(", 1)[1].split(")", 1)[0]
        return f"# Updated {path}\n\n- Raise spark.executor.memory to 8g\n"

    set_model_override(scripted_model(rewrite))
    async with get_sessionmaker()() as db:
        updated = await update_user_wiki(db, alice)
        await db.commit()
        # The repo name in the session matches the card despite different case/punctuation; unknown repos are skipped.
        assert sorted(updated) == ["decisions.md", "me/how-i-work.md", "projects/DataPipeline.md"]
        page = await find_page(db, alice.id, "private", "decisions.md")
        assert read_page(page).startswith("# Updated decisions.md")
        # Nothing new since the watermark: no changes.
        assert await update_user_wiki(db, alice) == []
        await db.commit()

    # An episode that lands after the previous run's query is picked up by the next run.
    async with get_sessionmaker()() as db:
        s2 = ChatSession(owner_id=alice.id, title="y")
        db.add(s2)
        await db.flush()
        db.add(Episode(session_id=s2.id, owner_id=alice.id, summary=SUMMARY, message_count=2))
        await db.commit()
        set_model_override(scripted_model("# How I work\n\n- Newer fact\n"))
        assert "me/how-i-work.md" in await update_user_wiki(db, alice)


async def test_librarian_changes_api(client_for):
    alice = await client_for("alice")
    async with get_sessionmaker()() as db:
        owner = await db.get(User, alice.user_id)
        await save_page(
            db,
            owner=owner,
            path="me/how-i-work.md",
            content="# How I work\n\nuv everywhere",
            author=LIBRARIAN,
            message="Librarian: update me/how-i-work.md from 1 session(s)",
        )
        await db.commit()
    changes = (await alice.get("/api/wiki/librarian/changes")).json()
    assert changes[0]["message"].startswith("Librarian:")
    assert "+uv everywhere" in changes[0]["diff"]
    assert (await alice.post("/api/wiki/librarian/run")).json() == {"queued": True}
    assert (await alice.post("/api/wiki/librarian/run")).json() == {"queued": False}


async def test_chat_context_includes_how_i_work_and_project_card(client_for):
    alice = await client_for("alice")
    seen: list[str] = []
    async with get_sessionmaker()() as db:
        owner = await db.get(User, alice.user_id)
        await save_page(db, owner=owner, path="me/how-i-work.md", content="# How I work\nPrefers uv.", author=LIBRARIAN)
        card = await save_page(
            db, owner=owner, path="projects/etl.md", content="# etl\nAirflow DAGs.", author=LIBRARIAN
        )
        p = Project(owner_id=owner.id, name="etl", repo_url="https://x/etl.git", host="gitlab", card_page_id=card.id)
        db.add(p)
        await db.commit()
        pid = p.id
    sid = (await alice.post("/api/chat/sessions", json={"project_id": pid})).json()["id"]

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    async def stream(messages, info):
        seen.append(info.instructions or "")
        yield "fine"

    set_model_override(FunctionModel(stream_function=stream))
    await alice.post(f"/api/chat/sessions/{sid}/messages", json={"content": "hi"})
    assert "Prefers uv." in seen[0] and "Airflow DAGs." in seen[0]
    assert ModelResponse and TextPart  # imported for clarity of the model protocol


async def test_episode_for_missing_or_empty_session(make_user):
    alice = await make_user("alice")
    import uuid

    async with get_sessionmaker()() as db:
        assert await build_episode(db, uuid.uuid4()) is None
        s = ChatSession(owner_id=alice.id, title="empty")
        db.add(s)
        await db.commit()
        assert await build_episode(db, s.id) is None
        await db.execute(update(ChatSession).values(title="x"))


async def test_sweep_skips_empty_sessions(make_user):
    alice = await make_user("alice")
    await _session_with_messages(alice, n=0)
    async with get_sessionmaker()() as db:
        assert await sessions_needing_episodes(db) == []


async def test_episode_survives_project_removal(client_for, git_integrations):
    alice = await client_for("alice")
    bob = await client_for("bob")
    pid = (
        await alice.post("/api/projects", json={"repo_url": "https://gitlab.example.com/t/etl.git", "host": "gitlab"})
    ).json()["id"]
    await alice.post(f"/api/projects/{pid}/visibility", json={"visibility": "public"})
    sid = (await bob.post("/api/chat/sessions", json={"project_id": pid})).json()["id"]
    async with get_sessionmaker()() as db:
        db.add(Message(session_id=sid, role="user", content="etl question"))
        await db.commit()
    set_model_override(scripted_model(structured=SUMMARY))
    async with get_sessionmaker()() as db:
        await build_episode(db, sid)
        await db.commit()
    assert (await alice.delete(f"/api/projects/{pid}")).status_code == 204
    hits = (await bob.get("/api/knowledge/search", params={"q": "SPIN-4412", "scope": "sessions"})).json()
    assert hits and hits[0]["project_id"] is None
