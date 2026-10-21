"""Repair drift between the wiki's git repos (source of truth) and the database (design 01 §H7, ADR-5).

Runs nightly, on worker start, and as `mwb wiki-reconcile [--dry-run]`.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.audit.service import audit
from app.config import get_settings
from app.db import get_sessionmaker
from app.knowledge.chunking import chunk_markdown
from app.knowledge.gitrepo import GitRepo
from app.knowledge.indexer import embed_prepared, prepare_document, set_visibility, write_document
from app.knowledge.wiki import find_page, read_page, repo_for, title_of
from app.logs import log
from app.models import Document, SystemState, User, WikiPage


@dataclass
class Report:
    checked: int = 0
    resynced: list[int] = field(default_factory=list)  # head_sha/title refreshed and re-indexed
    visibility_fixed: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    restored: list[int] = field(default_factory=list)  # was missing, file is back
    adopted: list[str] = field(default_factory=list)  # files with no row: row created
    skipped_files: list[str] = field(default_factory=list)
    dry_run: bool = False


def _other(visibility: str) -> str:
    return "private" if visibility == "public" else "public"


async def reconcile_wiki(*, dry_run: bool = False) -> Report:
    report = Report(dry_run=dry_run)
    reindex: list[int] = []
    root = get_settings().wiki_dir
    async with get_sessionmaker()() as db:
        pages = list(await db.scalars(select(WikiPage).order_by(WikiPage.id)))
        known: set[tuple[str, int | None, str]] = set()
        for page in pages:
            report.checked += 1
            expected = repo_for(page.visibility, page.owner_id)
            if expected.read(page.path) is not None:
                last = await expected.last_commit(page.path)
                if page.status != "ok":
                    page.status = "ok"
                    report.restored.append(page.id)
                if last and page.head_sha != last:
                    page.head_sha = last
                    page.title = title_of(page.path, expected.read(page.path) or "")
                    report.resynced.append(page.id)
                    reindex.append(page.id)
            else:
                other_vis = _other(page.visibility)
                other = repo_for(other_vis, page.owner_id)
                occupied = await find_page(db, page.owner_id, other_vis, page.path)
                if other.read(page.path) is not None and occupied is None:
                    page.visibility = other_vis
                    page.head_sha = await other.last_commit(page.path)
                    page.status = "ok"
                    if page.document_id and (doc := await db.get(Document, page.document_id)):
                        await set_visibility(db, doc, other_vis, actor_id=page.owner_id)
                    await audit(
                        db,
                        "wiki.reconcile.visibility",
                        user_id=None,
                        target_type="wiki_page",
                        target_id=page.id,
                        detail={"path": page.path, "visibility": other_vis},
                    )
                    report.visibility_fixed.append(page.id)
                elif page.status != "missing":
                    page.status = "missing"
                    report.missing.append(page.id)
            known.add((page.visibility, None if page.visibility == "public" else page.owner_id, page.path))

        # Files that have no row.
        user_ids = set(await db.scalars(select(User.id)))
        first_admin = await db.scalar(select(User.id).where(User.role == "admin").order_by(User.id))
        repos: list[tuple[str, int | None, GitRepo]] = [("public", None, GitRepo(root / "public"))]
        users_dir = root / "users"
        if users_dir.exists():
            for d in sorted(users_dir.iterdir()):
                if d.is_dir() and d.name.isdigit() and int(d.name) in user_ids:
                    repos.append(("private", int(d.name), GitRepo(d)))
        for visibility, owner_id, repo in repos:
            for path in await repo.list_markdown():
                if (visibility, owner_id, path) in known:
                    continue
                owner = owner_id if owner_id is not None else first_admin
                label = f"{repo.root.relative_to(root)}/{path}"
                if owner is None:
                    report.skipped_files.append(label)
                    continue
                page = WikiPage(
                    owner_id=owner,
                    visibility=visibility,
                    path=path,
                    title=title_of(path, repo.read(path) or ""),
                    head_sha=await repo.last_commit(path),
                )
                db.add(page)
                await db.flush()
                report.adopted.append(label)
                reindex.append(page.id)

        if dry_run:
            await db.rollback()
            return report
        await db.commit()

    for page_id in reindex:  # rule R1: embedding outside any transaction
        async with get_sessionmaker()() as db:
            page = await db.get(WikiPage, page_id)
            if page is None:
                continue
            content = read_page(page)
            p = prepare_document(
                owner_id=page.owner_id,
                source_type="wiki",
                source_ref=f"wiki:{page.id}",
                title=page.title,
                chunks=chunk_markdown(content),
            )
            await db.commit()
            emb = await embed_prepared(p)
            doc = await write_document(db, p, emb, visibility=page.visibility, meta={"path": page.path})
            page.document_id = doc.id
            await db.commit()

    await _record(report)
    log.info(
        "wiki.reconcile",
        checked=report.checked,
        resynced=len(report.resynced),
        visibility_fixed=len(report.visibility_fixed),
        missing=len(report.missing),
        adopted=len(report.adopted),
    )
    return report


async def _record(report: Report) -> None:
    value = {**asdict(report), "at": datetime.now(UTC).isoformat()}
    async with get_sessionmaker()() as db:
        await db.execute(
            insert(SystemState)
            .values(key="wiki_reconcile", value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": datetime.now(UTC)})
        )
        await db.commit()
