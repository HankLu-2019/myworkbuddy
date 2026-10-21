"""Librarian guardrails, conflict retry, run history and the per-timezone tick (design 01 §H8)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.knowledge import librarian as librarian_mod
from app.knowledge.librarian import nightly_start, update_user_wiki, users_due
from app.knowledge.librarian_guard import clean_output, rejection_reason, unified_diff
from app.knowledge.wiki import LIBRARIAN, author_of, find_page, read_page, save_page
from app.llm.models import set_model_override
from app.models import ChatSession, Episode, LibrarianRun, User
from app.tasks import app as tasks_mod
from tests.helpers import scripted_model
from tests.test_memory import SUMMARY

SECTIONS = ["Tools", "Habits", "Reviews", "Testing", "Deploys"]
BIG = "# How I work\n\n" + "".join(f"## {h}\n\n- {h.lower()}: {'detail ' * 15}\n\n" for h in SECTIONS)
KEEP = "<!-- mwb:keep -->\nNever touch prod on Fridays.\n<!-- /mwb:keep -->"


# ---------- pure rules ----------


def test_clean_output_strips_one_wrapping_fence() -> None:
    assert clean_output("```markdown\n# T\n\nbody\n```") == "# T\n\nbody\n"
    assert clean_output("# T\n\n```py\nx\n```\n") == "# T\n\n```py\nx\n```\n"


def test_accepts_a_normal_update() -> None:
    assert len(BIG) > 500
    assert rejection_reason(BIG, BIG + "## New\n\n- more\n", existing_page=True) is None


def test_rejects_shrink_growth_and_size() -> None:
    assert "shrink" in rejection_reason(BIG, BIG[: len(BIG) // 2], existing_page=True)
    assert "grow" in rejection_reason(BIG, BIG * 4, existing_page=True)
    assert "limit" in rejection_reason("", "x" * 60_001, existing_page=False)
    # Small pages may change freely, title included.
    assert rejection_reason("# DataPipeline\n\nOld card.", "# Data pipeline\n\nNew.", existing_page=True) is None


def test_rejects_dropped_headings() -> None:
    renamed = BIG
    for h in SECTIONS[:2]:
        renamed = renamed.replace(f"## {h}", f"## Other {h}")
    assert "drops 2 of 6 headings" in rejection_reason(BIG, renamed, existing_page=True)
    # Case and trailing hashes don't count as a change.
    assert rejection_reason(BIG, BIG.replace("## Tools", "## TOOLS ##"), existing_page=True) is None


def test_keep_blocks_must_survive_verbatim() -> None:
    current = f"# Me\n\n{KEEP}\n"
    assert rejection_reason(current, f"# Me\n\n{KEEP}\n\n- new\n", existing_page=True) is None
    changed = KEEP.replace("Fridays", "Mondays")
    assert "protected" in rejection_reason(current, f"# Me\n\n{changed}\n", existing_page=True)
    assert "protected" in rejection_reason(current, "# Me\n", existing_page=True)


def test_unified_diff_is_capped() -> None:
    d = unified_diff("a\n", "b\n" * 50_000, "me/x.md")
    assert d.startswith("--- a/me/x.md") and len(d) == 20_000


# ---------- update_user_wiki ----------


async def _user_with_page(make_user, content: str) -> User:
    user = await make_user("alice")
    async with get_sessionmaker()() as db:
        await save_page(db, owner=user, path="me/how-i-work.md", content=content, author=LIBRARIAN)
        s = ChatSession(owner_id=user.id, title="x")
        db.add(s)
        await db.flush()
        db.add(Episode(session_id=s.id, owner_id=user.id, message_count=2, summary={**SUMMARY, "decisions": []}))
        await db.commit()
    return user


async def _runs(user: User) -> list[LibrarianRun]:
    async with get_sessionmaker()() as db:
        return list(await db.scalars(select(LibrarianRun).where(LibrarianRun.user_id == user.id)))


async def test_rejected_rewrite_leaves_page_and_is_recorded(make_user) -> None:
    user = await _user_with_page(make_user, f"{BIG}\n{KEEP}\n")
    set_model_override(scripted_model("# How I work\n\n- everything is gone\n"))
    async with get_sessionmaker()() as db:
        assert await update_user_wiki(db, user) == []
        await db.commit()
        page = await find_page(db, user.id, "private", "me/how-i-work.md")
        assert "Never touch prod on Fridays" in read_page(page)
    [run] = await _runs(user)
    assert run.status == "partial" and run.episodes == 1 and run.pages_updated == []
    [rej] = run.pages_rejected
    assert rej["path"] == "me/how-i-work.md" and "protected" in rej["reason"]
    assert "-Never touch prod on Fridays." in rej["diff"]


async def test_accepted_rewrite_is_saved_and_recorded(make_user) -> None:
    user = await _user_with_page(make_user, BIG)
    set_model_override(scripted_model(f"```markdown\n{BIG}## Spark\n\n- Raise executor memory first\n```"))
    async with get_sessionmaker()() as db:
        assert await update_user_wiki(db, user) == ["me/how-i-work.md"]
        await db.commit()
        page = await find_page(db, user.id, "private", "me/how-i-work.md")
        text = read_page(page)
        assert "Raise executor memory first" in text and "```" not in text
    [run] = await _runs(user)
    assert run.status == "ok" and run.pages_updated == ["me/how-i-work.md"] and run.pages_rejected == []
    assert run.finished_at is not None


async def test_concurrent_user_edit_is_retried_once(make_user) -> None:
    user = await _user_with_page(make_user, BIG)
    prompts_seen: list[str] = []

    async def user_edits_during_first_call() -> None:
        async with get_sessionmaker()() as db:
            page = await find_page(db, user.id, "private", "me/how-i-work.md")
            await save_page(
                db,
                owner=user,
                path=page.path,
                content=BIG + "## Mine\n\n- typed by me\n",
                author=author_of(user),
                page=page,
            )
            await db.commit()

    def model(prompt: str) -> str:
        prompts_seen.append(prompt)
        return prompt.split("\n\n", 1)[1].split("\n\nNew session summaries", 1)[0] + "\n## Spark\n\n- added\n"

    set_model_override(scripted_model(model))
    orig_agent_run = librarian_mod.Agent.run

    async def run_then_edit(self, *a, **kw):
        result = await orig_agent_run(self, *a, **kw)
        if len(prompts_seen) == 1:
            await user_edits_during_first_call()
        return result

    librarian_mod.Agent.run = run_then_edit
    try:
        async with get_sessionmaker()() as db:
            assert await update_user_wiki(db, user) == ["me/how-i-work.md"]
            await db.commit()
            page = await find_page(db, user.id, "private", "me/how-i-work.md")
            text = read_page(page)
    finally:
        librarian_mod.Agent.run = orig_agent_run
    assert len(prompts_seen) == 2 and "typed by me" in prompts_seen[1]
    assert "typed by me" in text and "- added" in text


async def test_no_episodes_records_a_skipped_run(make_user) -> None:
    user = await make_user("bob")
    async with get_sessionmaker()() as db:
        assert await update_user_wiki(db, user) == []
        await db.commit()
    [run] = await _runs(user)
    assert run.status == "skipped" and run.episodes == 0


async def test_task_records_failed_run(make_user, monkeypatch) -> None:
    user = await make_user("carol")

    async def boom(db, u):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(tasks_mod, "update_user_wiki", boom)
    with pytest.raises(RuntimeError):
        await tasks_mod.librarian_user_task(user_id=user.id)
    [run] = await _runs(user)
    assert run.status == "failed" and run.error == "gateway down"


async def test_runs_api(client_for) -> None:
    alice = await client_for("alice")
    async with get_sessionmaker()() as db:
        user = await db.get(User, alice.user_id)
        await update_user_wiki(db, user)
        await db.commit()
    other = await client_for("bob")
    [run] = (await alice.get("/api/wiki/librarian/runs")).json()
    assert run["status"] == "skipped" and run["pages_rejected"] == []
    assert (await other.get("/api/wiki/librarian/runs")).json() == []


# ---------- scheduling in the user's timezone ----------


def _user(tz: str) -> User:
    return User(id=1, username="u", timezone=tz, is_active=True)


@pytest.mark.parametrize(
    ("now", "due"),
    [
        # Normal winter day: 02:00 CET = 01:00 UTC.
        (datetime(2026, 1, 10, 0, 59, tzinfo=UTC), None),
        (datetime(2026, 1, 10, 1, 0, tzinfo=UTC), datetime(2026, 1, 10, 1, 0, tzinfo=UTC)),
        # Spring forward 2026-03-29: 02:00 local doesn't exist; due at 03:00 CEST = 01:00 UTC.
        (datetime(2026, 3, 29, 0, 30, tzinfo=UTC), None),
        (datetime(2026, 3, 29, 1, 5, tzinfo=UTC), datetime(2026, 3, 29, 1, 0, tzinfo=UTC)),
        # Fall back 2026-10-25: 02:00 happens twice; the first (CEST, 00:00 UTC) counts.
        (datetime(2026, 10, 25, 0, 5, tzinfo=UTC), datetime(2026, 10, 25, 0, 0, tzinfo=UTC)),
        (datetime(2026, 10, 25, 1, 5, tzinfo=UTC), datetime(2026, 10, 25, 0, 0, tzinfo=UTC)),
    ],
)
def test_nightly_start_stockholm(now: datetime, due: datetime | None) -> None:
    assert nightly_start(_user("Europe/Stockholm"), now) == due


def test_nightly_start_other_zones() -> None:
    now = datetime(2026, 1, 10, 7, 30, tzinfo=UTC)
    assert nightly_start(_user("America/New_York"), now) == datetime(2026, 1, 10, 7, 0, tzinfo=UTC)
    assert nightly_start(_user("Asia/Tokyo"), now) == datetime(2026, 1, 9, 17, 0, tzinfo=UTC)
    assert nightly_start(_user("Not/AZone"), now) == datetime(2026, 1, 10, 2, 0, tzinfo=UTC)  # falls back to UTC


async def test_tick_defers_each_due_user_once(make_user, task_queue, monkeypatch) -> None:
    sthlm = await make_user("alice")
    tokyo = await make_user("kenji")
    gone = await make_user("old")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == sthlm.id).values(timezone="Europe/Stockholm"))
        await db.execute(update(User).where(User.id == tokyo.id).values(timezone="Asia/Tokyo"))
        await db.execute(update(User).where(User.id == gone.id).values(timezone="UTC", is_active=False))
        await db.commit()
    now = datetime(2026, 10, 21, 1, 5, tzinfo=UTC)  # 03:05 Stockholm, 10:05 Tokyo
    monkeypatch.setattr(tasks_mod, "_now", lambda: now)
    async with get_sessionmaker()() as db:
        assert sorted(await users_due(db, now)) == sorted([sthlm.id, tokyo.id])
        # Tokyo already ran after its 02:00 (= 17:00 UTC yesterday).
        db.add(
            LibrarianRun(
                user_id=tokyo.id,
                started_at=datetime(2026, 10, 20, 17, 30, tzinfo=UTC),
                local_date=datetime(2026, 10, 21).date(),
                status="ok",
            )
        )
        await db.commit()
    await tasks_mod.librarian_tick(timestamp=0)
    await tasks_mod.librarian_tick(timestamp=0)  # the queued job de-duplicates
    jobs = [j for j in task_queue.jobs.values() if j["task_name"] == "librarian_user"]
    assert [j["args"]["user_id"] for j in jobs] == [sthlm.id]


def test_librarian_hour_is_validated(monkeypatch) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("MWB_LIBRARIAN_LOCAL_HOUR", "24")
    with pytest.raises(ValidationError):
        Settings()
