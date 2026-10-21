import re
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge.acl import Visibility, can_change_visibility
from app.knowledge.chunking import chunk_markdown
from app.knowledge.gitrepo import Author, GitRepo, validate_page_path
from app.knowledge.indexer import (
    PreparedDoc,
    commit_before_network,
    embed_prepared,
    prepare_document,
    set_visibility,
    write_document,
)
from app.models import Document, User, WikiPage

LIBRARIAN = Author("MyWorkBuddy Librarian", "librarian@myworkbuddy.local")


def author_of(user: User) -> Author:
    return Author(user.display_name or user.username, user.email or f"{user.username}@myworkbuddy.local")


def repo_for(visibility: str, owner_id: int) -> GitRepo:
    root: Path = get_settings().wiki_dir
    return GitRepo(root / "public" if visibility == "public" else root / "users" / str(owner_id))


def title_of(path: str, content: str) -> str:
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return m.group(1).strip()[:255] if m else Path(path).stem.replace("-", " ").replace("_", " ")


async def _lock(db: AsyncSession, repo: GitRepo) -> None:
    # Serializes git writes to one repo across api and worker processes until the transaction ends.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": str(repo.root)})


async def find_page(db: AsyncSession, owner_id: int, visibility: str, path: str) -> WikiPage | None:
    q = select(WikiPage).where(WikiPage.path == path, WikiPage.visibility == visibility)
    if visibility == "private":
        q = q.where(WikiPage.owner_id == owner_id)
    return await db.scalar(q)


async def _write_index(db: AsyncSession, page: WikiPage, p: PreparedDoc, emb) -> None:
    p.source_ref = f"wiki:{page.id}"
    doc = await write_document(db, p, emb, visibility=page.visibility, meta={"path": page.path})
    page.document_id = doc.id


async def save_page(
    db: AsyncSession,
    *,
    owner: User,
    path: str,
    content: str,
    author: Author,
    message: str | None = None,
    page: WikiPage | None = None,
) -> WikiPage:
    """Create a private page for `owner`, or update `page` in whichever repo it lives in.

    Commits the caller's session before embedding (rule R1); the caller commits the page write.
    """
    path = validate_page_path(path)
    if page is None:
        page = await find_page(db, owner.id, "private", path)
    title = title_of(path, content)
    prepared = prepare_document(
        owner_id=page.owner_id if page else owner.id,
        source_type="wiki",
        source_ref=f"wiki:{page.id}" if page else None,
        title=title,
        chunks=chunk_markdown(content),
    )
    await commit_before_network(db)
    emb = await embed_prepared(prepared)

    if page is None:
        page = WikiPage(owner_id=owner.id, visibility="private", path=path, title=title)
        db.add(page)
        await db.flush()
    repo = repo_for(page.visibility, page.owner_id)
    await _lock(db, repo)
    page.title = title
    page.head_sha = await repo.write(path, content, author, message or f"Update {path}")
    await _write_index(db, page, prepared, emb)
    return page


def read_page(page: WikiPage) -> str:
    return repo_for(page.visibility, page.owner_id).read(page.path) or ""


async def change_visibility(db: AsyncSession, page: WikiPage, actor: User, target: Visibility) -> WikiPage:
    if page.visibility == target:
        return page
    if not can_change_visibility(page, actor, target):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can publish this page")
    if await find_page(db, page.owner_id, target, page.path):
        where = "in the team wiki" if target == "public" else "in the owner's private wiki"
        raise HTTPException(status.HTTP_409_CONFLICT, f"A page at {page.path} already exists {where}")
    src, dst = repo_for(page.visibility, page.owner_id), repo_for(target, page.owner_id)
    await _lock(db, src)
    await _lock(db, dst)
    content = read_page(page)
    verb = "Publish" if target == "public" else "Unpublish"
    author = author_of(actor)
    page.head_sha = await dst.write(page.path, content, author, f"{verb} {page.path}")
    await src.remove(page.path, author, f"{verb} {page.path} (moved)")
    page.visibility = target
    if page.document_id and (doc := await db.get(Document, page.document_id)):
        await set_visibility(db, doc, target, actor_id=actor.id)
    await db.flush()
    return page


async def delete_page(db: AsyncSession, page: WikiPage, actor: User) -> None:
    repo = repo_for(page.visibility, page.owner_id)
    await _lock(db, repo)
    await repo.remove(page.path, author_of(actor), f"Delete {page.path}")
    if page.document_id and (doc := await db.get(Document, page.document_id)):
        await db.delete(doc)
    await db.delete(page)
