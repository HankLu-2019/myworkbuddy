"""Removing a user (design 01 §H11): team items move to an admin, private data is deleted.

"Team items" are rows with visibility = 'public': documents (and their chunks), wiki pages, projects, chat
sessions and episodes. Everything else the user owns is deleted by the users FK cascades. Files on disk are
removed afterwards by tasks (`cleanup_user_files`, `cleanup_project_cache`), under the same locks as the jobs
that write them.

Conflicts with the new owner's own rows:
- a public project whose repository the new owner already added is not transferred (it is deleted, together
  with its code index); the new owner's project stays;
- documents and imported sessions that would collide on their unique keys get a `#from-<old id>` suffix.
"""

from dataclasses import dataclass, field

from sqlalchemy import and_, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.audit.service import audit
from app.models import (
    Artifact,
    AuthSession,
    ChatSession,
    Chunk,
    Document,
    Episode,
    Project,
    User,
    UserCredential,
    UserSecret,
    WikiPage,
)

CODE_TYPES = ("code", "repo_doc")


@dataclass
class Plan:
    transfer: dict[str, int] = field(default_factory=dict)
    delete: dict[str, int] = field(default_factory=dict)
    kept_duplicate_projects: list[int] = field(default_factory=list)  # public, but the new owner has the repo
    deleted_project_ids: list[int] = field(default_factory=list)  # their repo caches must be removed


async def _count(db: AsyncSession, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def _duplicate_projects(db: AsyncSession, user_id: int, new_owner_id: int) -> list[int]:
    mine = aliased(Project)
    return list(
        await db.scalars(
            select(Project.id).where(
                Project.owner_id == user_id,
                Project.visibility == "public",
                exists().where(and_(mine.owner_id == new_owner_id, mine.repo_url == Project.repo_url)),
            )
        )
    )


def _transferable_docs(user_id: int, skipped_projects: list[int]):
    cond = [Document.owner_id == user_id, Document.visibility == "public"]
    if skipped_projects:
        cond.append(~and_(Document.project_id.in_(skipped_projects), Document.source_type.in_(CODE_TYPES)))
    return cond


async def plan(db: AsyncSession, user: User, new_owner: User) -> Plan:
    """Counts for the confirmation dialog; `offboard()` moves and deletes exactly these."""
    uid = user.id
    skipped = await _duplicate_projects(db, uid, new_owner.id)
    public_sessions = select(ChatSession.id).where(ChatSession.owner_id == uid, ChatSession.visibility == "public")
    p = Plan(kept_duplicate_projects=skipped)
    p.transfer = {
        "documents": await _count(db, Document, *_transferable_docs(uid, skipped)),
        "wiki_pages": await _count(db, WikiPage, WikiPage.owner_id == uid, WikiPage.visibility == "public"),
        "projects": await _count(
            db, Project, Project.owner_id == uid, Project.visibility == "public", Project.id.not_in(skipped or [0])
        ),
        "chat_sessions": await _count(db, ChatSession, ChatSession.id.in_(public_sessions)),
        "episodes": await _count(
            db,
            Episode,
            Episode.owner_id == uid,
            Episode.visibility == "public",
            Episode.session_id.in_(public_sessions),
        ),
    }
    total_docs = await _count(db, Document, Document.owner_id == uid)
    p.deleted_project_ids = list(
        await db.scalars(
            select(Project.id).where(
                Project.owner_id == uid, (Project.visibility != "public") | Project.id.in_(skipped or [0])
            )
        )
    )
    p.delete = {
        "documents": total_docs - p.transfer["documents"],
        "wiki_pages": await _count(db, WikiPage, WikiPage.owner_id == uid) - p.transfer["wiki_pages"],
        "projects": len(p.deleted_project_ids),
        "chat_sessions": await _count(db, ChatSession, ChatSession.owner_id == uid) - p.transfer["chat_sessions"],
        "episodes": await _count(db, Episode, Episode.owner_id == uid) - p.transfer["episodes"],
        "artifacts": await _count(db, Artifact, Artifact.owner_id == uid),
        "secrets": await _count(db, UserSecret, UserSecret.user_id == uid)
        + await _count(db, UserCredential, UserCredential.user_id == uid),
        "sign_ins": await _count(db, AuthSession, AuthSession.user_id == uid),
    }
    return p


async def offboard(db: AsyncSession, user: User, new_owner: User, *, actor_id: int, ip: str | None = None) -> Plan:
    """Transfer team items, then delete the user (cascades the rest). One transaction; the caller commits."""
    uid, new = user.id, new_owner.id
    result = await plan(db, user, new_owner)
    skipped = result.kept_duplicate_projects
    suffix = f"#from-{uid}"

    async def audited(kind: str, ids: list) -> None:
        for target_id in ids:
            await audit(
                db,
                "offboard.transfer",
                user_id=actor_id,
                target_type=kind,
                target_id=str(target_id),
                detail={"from_user": uid, "to_user": new},
                ip=ip,
            )

    # Documents (+ chunks). A document whose unique key the new owner already uses gets a suffixed ref.
    doc_ids = list(await db.scalars(select(Document.id).where(*_transferable_docs(uid, skipped))))
    if doc_ids:
        await db.execute(
            text(
                "UPDATE documents d SET owner_id = :new, source_ref = CASE WHEN EXISTS ("
                "  SELECT 1 FROM documents o WHERE o.owner_id = :new AND o.source_type = d.source_type"
                "  AND o.source_ref = d.source_ref) THEN d.source_ref || :suffix ELSE d.source_ref END "
                "WHERE d.id = ANY(:ids)"
            ),
            {"new": new, "suffix": suffix, "ids": doc_ids},
        )
        await db.execute(update(Chunk).where(Chunk.document_id.in_(doc_ids)).values(owner_id=new))
    await audited("document", doc_ids)

    page_ids = list(
        await db.scalars(select(WikiPage.id).where(WikiPage.owner_id == uid, WikiPage.visibility == "public"))
    )
    if page_ids:  # public pages live in the shared repo; only the DB owner changes
        await db.execute(update(WikiPage).where(WikiPage.id.in_(page_ids)).values(owner_id=new))
    await audited("wiki_page", page_ids)

    project_ids = list(
        await db.scalars(
            select(Project.id).where(
                Project.owner_id == uid, Project.visibility == "public", Project.id.not_in(skipped or [0])
            )
        )
    )
    if project_ids:
        await db.execute(update(Project).where(Project.id.in_(project_ids)).values(owner_id=new))
        # A card that was a private page is deleted with the user; don't leave a dangling id.
        await db.execute(
            update(Project)
            .where(
                Project.id.in_(project_ids),
                Project.card_page_id.is_not(None),
                ~exists().where(and_(WikiPage.id == Project.card_page_id, WikiPage.owner_id == new)),
            )
            .values(card_page_id=None)
        )
    await audited("project", project_ids)

    session_ids = list(
        await db.scalars(select(ChatSession.id).where(ChatSession.owner_id == uid, ChatSession.visibility == "public"))
    )
    if session_ids:
        await db.execute(
            text(
                "UPDATE chat_sessions s SET owner_id = :new, external_ref = CASE WHEN s.external_ref IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM chat_sessions o WHERE o.owner_id = :new AND o.source = s.source "
                "AND o.external_ref = s.external_ref) THEN s.external_ref || :suffix ELSE s.external_ref END "
                "WHERE s.id = ANY(:ids)"
            ),
            {"new": new, "suffix": suffix, "ids": session_ids},
        )
        await db.execute(
            update(Episode)
            .where(Episode.owner_id == uid, Episode.visibility == "public", Episode.session_id.in_(session_ids))
            .values(owner_id=new)
        )
    await audited("chat_session", session_ids)

    await audit(
        db,
        "user.delete",
        user_id=actor_id,
        target_type="user",
        target_id=str(uid),
        detail={
            "username": user.username,
            "transfer_to": new,
            "transferred": result.transfer,
            "deleted": result.delete,
        },
        ip=ip,
    )
    await db.delete(user)  # FK cascades: private items, secrets, sign-ins, librarian state and runs
    await db.flush()
    return result
