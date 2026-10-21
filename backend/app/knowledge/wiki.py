import re
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge.acl import Visibility, can_change_visibility
from app.knowledge.chunking import chunk_markdown
from app.knowledge.gitrepo import Author, Commit, GitRepo, validate_page_path
from app.knowledge.indexer import (
    PreparedDoc,
    commit_before_network,
    embed_prepared,
    prepare_document,
    set_visibility,
    write_document,
)
from app.knowledge.wiki_writer import WikiWriter
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
    base_sha: str | None = None,
    writer: WikiWriter | None = None,
) -> WikiPage:
    """Create a private page for `owner`, or update `page` in whichever repo it lives in.

    Embeds before taking the lock (rule R1). With `base_sha`, raises 409 wiki_conflict if the page changed since.
    Without `writer`, runs in its own WikiWriter and commits; with one, the caller commits inside its block.
    """
    if writer is None:
        async with WikiWriter() as w:
            saved = await save_page(
                db, owner=owner, path=path, content=content, author=author, message=message, page=page,
                base_sha=base_sha, writer=w,
            )  # fmt: skip
            await db.commit()
            return saved

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

    repo = repo_for(page.visibility if page else "private", page.owner_id if page else owner.id)
    await writer.lock(repo)
    if page is not None:
        await writer.expect(repo, path, base_sha, read_page(page))
    else:
        page = WikiPage(owner_id=owner.id, visibility="private", path=path, title=title)
        db.add(page)
        await db.flush()
    page.title = title
    page.status = "ok"
    page.head_sha = await writer.write(repo, path, content, author, message or f"Update {path}")
    await _write_index(db, page, prepared, emb)
    return page


def read_page(page: WikiPage) -> str:
    return repo_for(page.visibility, page.owner_id).read(page.path) or ""


async def change_visibility(
    db: AsyncSession, page: WikiPage, actor: User, target: Visibility, *, writer: WikiWriter
) -> WikiPage:
    """Move the page file between the private and team repos. The caller commits inside `writer`'s block."""
    if page.visibility == target:
        return page
    if not can_change_visibility(page, actor, target):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can publish this page")
    if await find_page(db, page.owner_id, target, page.path):
        where = "in the team wiki" if target == "public" else "in the owner's private wiki"
        raise HTTPException(status.HTTP_409_CONFLICT, f"A page at {page.path} already exists {where}")
    src, dst = repo_for(page.visibility, page.owner_id), repo_for(target, page.owner_id)
    await writer.lock(src, dst)
    content = read_page(page)
    source_sha = await src.last_commit(page.path)
    verb = "Publish" if target == "public" else "Unpublish"
    author = author_of(actor)
    trailer = f"\n\nMoved-From: {src.root.relative_to(get_settings().wiki_dir)}@{source_sha}" if source_sha else ""
    page.head_sha = await writer.write(dst, page.path, content, author, f"{verb} {page.path}{trailer}")
    await writer.remove(src, page.path, author, f"{verb} {page.path} (moved)")
    page.visibility = target
    if page.document_id and (doc := await db.get(Document, page.document_id)):
        await set_visibility(db, doc, target, actor_id=actor.id)
    await db.flush()
    return page


async def delete_page(db: AsyncSession, page: WikiPage, actor: User, *, writer: WikiWriter) -> None:
    """The caller commits inside `writer`'s block."""
    repo = repo_for(page.visibility, page.owner_id)
    await writer.lock(repo)
    if repo.read(page.path) is not None:
        await writer.remove(repo, page.path, author_of(actor), f"Delete {page.path}")
    if page.document_id and (doc := await db.get(Document, page.document_id)):
        await db.delete(doc)
    await db.delete(page)


async def page_history(page: WikiPage, viewer: User, limit: int = 50) -> list[Commit]:
    """History of the page's path, plus (one hop) its history before it was moved between repos.

    Pre-move history from a private repo is shown to the page owner only.
    """
    repo = repo_for(page.visibility, page.owner_id)
    commits = await repo.log(page.path, limit=limit)
    origin = commits[-1].moved_from if commits else None
    if origin and "@" in origin and len(commits) < limit:
        where, sha = origin.rsplit("@", 1)
        private_origin = where.startswith("users/")
        if not private_origin or viewer.id == page.owner_id:
            source = GitRepo(get_settings().wiki_dir / where)
            if (source.root / ".git").exists():
                commits += await source.log(page.path, limit=limit - len(commits), until=sha)
    return commits


async def repo_with_commit(page: WikiPage, viewer: User, sha: str) -> GitRepo | None:
    """The repo holding `sha` among those whose history `viewer` may see for this page (current, then origin)."""
    candidates = [repo_for(page.visibility, page.owner_id)]
    for c in await page_history(page, viewer):
        if c.moved_from and "@" in c.moved_from:
            where = c.moved_from.rsplit("@", 1)[0]
            if not where.startswith("users/") or viewer.id == page.owner_id:
                candidates.append(GitRepo(get_settings().wiki_dir / where))
    for repo in candidates:
        if await repo.has_commit(sha):
            return repo
    return None


async def page_version(page: WikiPage, viewer: User, sha: str) -> str | None:
    """Content of the page at `sha`, or None if the viewer can't see that version or it doesn't exist."""
    repo = await repo_with_commit(page, viewer, sha)
    return await repo.file_at(sha, page.path) if repo else None
