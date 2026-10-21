from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.audit.service import audit
from app.chat.agent import SCOPES
from app.deps import DB, CurrentAuth
from app.knowledge.acl import (
    Visibility,
    can_change_visibility,
    can_edit,
    ensure_edit,
    ensure_view,
    visible_filter,
)
from app.knowledge.gitrepo import validate_page_path, validate_sha
from app.knowledge.indexer import set_project_visibility, set_visibility
from app.knowledge.ingest.files import MAX_UPLOAD_BYTES, ingest_upload
from app.knowledge.ingest.repos import list_projects, project_name, validate_repo_url
from app.knowledge.ingest.transcripts import import_transcript
from app.knowledge.search import hybrid_search
from app.knowledge.wiki import (
    LIBRARIAN,
    author_of,
    change_visibility,
    delete_page,
    find_page,
    page_history,
    page_version,
    read_page,
    repo_for,
    repo_with_commit,
    save_page,
)
from app.knowledge.wiki_writer import WikiWriter
from app.models import Chunk, Document, Project, User, WikiPage
from app.tasks.app import build_episode_task, defer_once, ingest_project_task, librarian_user_task

router = APIRouter(prefix="/api", tags=["knowledge"])


class VisibilityIn(BaseModel):
    visibility: Visibility


# ---------- search ----------


class HitOut(BaseModel):
    document_id: int
    title: str
    source_type: str
    source_ref: str
    visibility: str
    owner_id: int
    project_id: int | None
    excerpt: str
    meta: dict[str, Any]
    score: float


@router.get("/knowledge/search", response_model=list[HitOut])
async def search(
    auth: CurrentAuth,
    db: DB,
    q: str,
    scope: Literal["all", "wiki", "sessions", "code", "documents"] = "all",
    project_id: int | None = None,
    limit: int = 12,
) -> list[HitOut]:
    hits = await hybrid_search(
        db, auth.user, q, source_types=SCOPES[scope], project_id=project_id, limit=max(1, min(limit, 50))
    )
    return [
        HitOut(
            document_id=h.document_id,
            title=h.title,
            source_type=h.source_type,
            source_ref=h.source_ref,
            visibility=h.visibility,
            owner_id=h.owner_id,
            project_id=h.project_id,
            excerpt=h.content[:800],
            meta=h.meta,
            score=h.score,
        )
        for h in hits
    ]


# ---------- documents (uploads) ----------


class DocumentOut(BaseModel):
    id: int
    title: str
    source_type: str
    source_ref: str
    visibility: str
    owner_id: int
    project_id: int | None
    chunks: int
    updated_at: datetime
    mine: bool


async def _document_out(db: DB, docs: list[Document], user: User) -> list[DocumentOut]:
    counts = (
        dict(
            (
                await db.execute(
                    select(Chunk.document_id, func.count())
                    .where(Chunk.document_id.in_([d.id for d in docs]))
                    .group_by(Chunk.document_id)
                )
            ).all()
        )
        if docs
        else {}
    )
    return [
        DocumentOut(
            id=d.id,
            title=d.title,
            source_type=d.source_type,
            source_ref=d.source_ref,
            visibility=d.visibility,
            owner_id=d.owner_id,
            project_id=d.project_id,
            chunks=counts.get(d.id, 0),
            updated_at=d.updated_at,
            mine=d.owner_id == user.id,
        )
        for d in docs
    ]


@router.get("/knowledge/documents", response_model=list[DocumentOut])
async def list_documents(auth: CurrentAuth, db: DB, source_type: str = "upload") -> list[DocumentOut]:
    docs = list(
        await db.scalars(
            select(Document)
            .where(visible_filter(Document, auth.user), Document.source_type == source_type)
            .order_by(Document.updated_at.desc())
            .limit(500)
        )
    )
    return await _document_out(db, docs, auth.user)


@router.post("/knowledge/documents", response_model=DocumentOut, status_code=201)
async def upload_document(auth: CurrentAuth, db: DB, file: UploadFile = File(...)) -> DocumentOut:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    doc = await ingest_upload(db, auth.user.id, file.filename or "upload.txt", data)
    await audit(db, "document.upload", user_id=auth.user.id, target_type="document", target_id=doc.id)
    await db.commit()
    return (await _document_out(db, [doc], auth.user))[0]


async def _upload_doc(db: DB, doc_id: int, user: User) -> Document:
    doc = await db.get(Document, doc_id)
    ensure_view(doc, user)
    if doc.source_type != "upload":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Manage this item from its wiki page, project or session")
    return doc


@router.post("/knowledge/documents/{doc_id}/visibility", response_model=DocumentOut)
async def document_visibility(doc_id: int, body: VisibilityIn, auth: CurrentAuth, db: DB) -> DocumentOut:
    doc = await _upload_doc(db, doc_id, auth.user)
    if not can_change_visibility(doc, auth.user, body.visibility):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can publish this document")
    await set_visibility(db, doc, body.visibility, actor_id=auth.user.id)
    await audit(db, f"document.{body.visibility}", user_id=auth.user.id, target_type="document", target_id=doc.id)
    await db.commit()
    return (await _document_out(db, [doc], auth.user))[0]


@router.delete("/knowledge/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: int, auth: CurrentAuth, db: DB) -> None:
    doc = await _upload_doc(db, doc_id, auth.user)
    ensure_edit(doc, auth.user)
    await db.delete(doc)
    await audit(db, "document.delete", user_id=auth.user.id, target_type="document", target_id=doc_id)
    await db.commit()


# ---------- wiki ----------


class PageSummary(BaseModel):
    id: int
    path: str
    title: str
    visibility: str
    owner_id: int
    updated_at: datetime
    mine: bool


class PageOut(PageSummary):
    content: str
    head_sha: str | None
    can_edit: bool


class PageCreate(BaseModel):
    path: str
    content: str = Field(max_length=500_000)
    message: str | None = Field(default=None, max_length=200)


class PageUpdate(BaseModel):
    content: str = Field(max_length=500_000)
    message: str | None = Field(default=None, max_length=200)
    # The page's head_sha when the editor loaded it; a different current version -> 409 wiki_conflict.
    base_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$")


class CommitOut(BaseModel):
    sha: str
    author: str
    date: str
    message: str


def _summary(p: WikiPage, user: User) -> PageSummary:
    return PageSummary(
        id=p.id,
        path=p.path,
        title=p.title,
        visibility=p.visibility,
        owner_id=p.owner_id,
        updated_at=p.updated_at,
        mine=p.owner_id == user.id,
    )


def _page_out(p: WikiPage, user: User) -> PageOut:
    return PageOut(
        **_summary(p, user).model_dump(), content=read_page(p), head_sha=p.head_sha, can_edit=can_edit(p, user)
    )


async def _page(db: DB, page_id: int, user: User) -> WikiPage:
    page = await db.get(WikiPage, page_id)
    ensure_view(page, user)
    return page


@router.get("/wiki/pages", response_model=list[PageSummary])
async def list_pages(auth: CurrentAuth, db: DB) -> list[PageSummary]:
    pages = await db.scalars(
        select(WikiPage)
        .where(visible_filter(WikiPage, auth.user), WikiPage.status == "ok")  # missing pages: see reconcile
        .order_by(WikiPage.visibility, WikiPage.path)
    )
    return [_summary(p, auth.user) for p in pages]


@router.get("/wiki/pages/{page_id}", response_model=PageOut)
async def get_page(page_id: int, auth: CurrentAuth, db: DB) -> PageOut:
    return _page_out(await _page(db, page_id, auth.user), auth.user)


@router.post("/wiki/pages", response_model=PageOut, status_code=201)
async def create_page(body: PageCreate, auth: CurrentAuth, db: DB) -> PageOut:
    path = validate_page_path(body.path)
    if await find_page(db, auth.user.id, "private", path):
        raise HTTPException(status.HTTP_409_CONFLICT, "You already have a page at this path")
    async with WikiWriter() as w:
        page = await save_page(
            db,
            owner=auth.user,
            path=path,
            content=body.content,
            author=author_of(auth.user),
            message=body.message or f"Create {path}",
            writer=w,
        )
        await db.commit()
    return _page_out(page, auth.user)


@router.put("/wiki/pages/{page_id}", response_model=PageOut)
async def update_page(page_id: int, body: PageUpdate, auth: CurrentAuth, db: DB) -> PageOut:
    page = await _page(db, page_id, auth.user)
    ensure_edit(page, auth.user)
    owner = await db.get(User, page.owner_id)
    async with WikiWriter() as w:
        page = await save_page(
            db,
            owner=owner,
            path=page.path,
            content=body.content,
            author=author_of(auth.user),
            message=body.message,
            page=page,
            base_sha=body.base_sha,
            writer=w,
        )
        await db.commit()
    return _page_out(page, auth.user)


@router.post("/wiki/pages/{page_id}/visibility", response_model=PageOut)
async def page_visibility(page_id: int, body: VisibilityIn, auth: CurrentAuth, db: DB) -> PageOut:
    page = await _page(db, page_id, auth.user)
    async with WikiWriter() as w:
        page = await change_visibility(db, page, auth.user, body.visibility, writer=w)
        await audit(
            db,
            f"wiki.{body.visibility}",
            user_id=auth.user.id,
            target_type="wiki_page",
            target_id=page.id,
            detail={"path": page.path},
        )
        await db.commit()
    return _page_out(page, auth.user)


@router.delete("/wiki/pages/{page_id}", status_code=204)
async def remove_page(page_id: int, auth: CurrentAuth, db: DB) -> None:
    page = await _page(db, page_id, auth.user)
    ensure_edit(page, auth.user)
    async with WikiWriter() as w:
        await audit(
            db,
            "wiki.delete",
            user_id=auth.user.id,
            target_type="wiki_page",
            target_id=page.id,
            detail={"path": page.path},
        )
        await delete_page(db, page, auth.user, writer=w)
        await db.commit()


@router.get("/wiki/pages/{page_id}/history", response_model=list[CommitOut])
async def get_page_history(page_id: int, auth: CurrentAuth, db: DB) -> list[CommitOut]:
    page = await _page(db, page_id, auth.user)
    commits = await page_history(page, auth.user)
    return [CommitOut(sha=c.sha, author=c.author, date=c.date, message=c.message) for c in commits]


@router.get("/wiki/pages/{page_id}/commits/{sha}")
async def page_diff(page_id: int, sha: str, auth: CurrentAuth, db: DB) -> dict[str, str]:
    page = await _page(db, page_id, auth.user)
    validate_sha(sha)
    repo = await repo_with_commit(page, auth.user, sha)
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such commit for this page")
    return {"diff": await repo.show(sha, page.path)}


class VersionOut(BaseModel):
    sha: str
    content: str


@router.get("/wiki/pages/{page_id}/versions/{sha}", response_model=VersionOut)
async def get_page_version(page_id: int, sha: str, auth: CurrentAuth, db: DB) -> VersionOut:
    page = await _page(db, page_id, auth.user)
    content = await page_version(page, auth.user, sha)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This page has no version at that commit")
    return VersionOut(sha=sha, content=content)


@router.get("/wiki/librarian/changes")
async def librarian_changes(auth: CurrentAuth, days: int = 3) -> list[dict[str, str]]:
    repo = repo_for("private", auth.user.id)
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 30)))
    commits = [c for c in await repo.log(limit=100, author=LIBRARIAN.email) if datetime.fromisoformat(c.date) >= since]
    return [{"sha": c.sha, "date": c.date, "message": c.message, "diff": await repo.show(c.sha)} for c in commits]


@router.post("/wiki/librarian/run", status_code=202)
async def run_librarian(auth: CurrentAuth) -> dict[str, bool]:
    return {"queued": await defer_once(librarian_user_task, f"librarian:{auth.user.id}", user_id=auth.user.id)}


# ---------- projects ----------


class ProjectOut(BaseModel):
    id: int
    name: str
    repo_url: str
    host: str
    branch: str | None
    visibility: str
    status: str
    error: str | None
    last_ingested_sha: str | None
    owner_id: int
    updated_at: datetime
    mine: bool


class ProjectCreate(BaseModel):
    repo_url: str
    host: Literal["gitlab", "gerrit"]
    branch: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._/-]{1,128}$")
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")


def _project_out(p: Project, user: User) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        repo_url=p.repo_url,
        host=p.host,
        branch=p.branch,
        visibility=p.visibility,
        status=p.status,
        error=p.error if p.owner_id == user.id else None,
        last_ingested_sha=p.last_ingested_sha,
        owner_id=p.owner_id,
        updated_at=p.updated_at,
        mine=p.owner_id == user.id,
    )


async def _own_project(db: DB, project_id: int, user: User) -> Project:
    project = await db.get(Project, project_id)
    ensure_view(project, user)
    ensure_edit(project, user)
    return project


@router.get("/projects", response_model=list[ProjectOut])
async def get_projects(auth: CurrentAuth, db: DB) -> list[ProjectOut]:
    return [_project_out(p, auth.user) for p in await list_projects(db, auth.user)]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def add_project(body: ProjectCreate, auth: CurrentAuth, db: DB) -> ProjectOut:
    url = validate_repo_url(body.repo_url)
    if await db.scalar(select(Project).where(Project.owner_id == auth.user.id, Project.repo_url == url)):
        raise HTTPException(status.HTTP_409_CONFLICT, "You already added this repository")
    project = Project(
        owner_id=auth.user.id,
        repo_url=url,
        host=body.host,
        branch=body.branch,
        name=body.name or project_name(url),
        status="queued",
    )
    db.add(project)
    await db.flush()
    await audit(db, "project.add", user_id=auth.user.id, target_type="project", target_id=project.id)
    await db.commit()
    await defer_once(ingest_project_task, f"ingest:{project.id}", project_id=project.id)
    return _project_out(project, auth.user)


@router.post("/projects/{project_id}/ingest", status_code=202)
async def reingest(project_id: int, auth: CurrentAuth, db: DB, force: bool = False) -> dict[str, bool]:
    project = await _own_project(db, project_id, auth.user)
    queued = await defer_once(ingest_project_task, f"ingest:{project.id}", project_id=project.id, force=force)
    if queued:
        project.status = "queued"
        await db.commit()
    return {"queued": queued}


@router.post("/projects/{project_id}/visibility", response_model=ProjectOut)
async def project_visibility(project_id: int, body: VisibilityIn, auth: CurrentAuth, db: DB) -> ProjectOut:
    project = await db.get(Project, project_id)
    ensure_view(project, auth.user)
    if not can_change_visibility(project, auth.user, body.visibility):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can publish this project")
    await set_project_visibility(db, project, body.visibility, actor_id=auth.user.id)
    await audit(db, f"project.{body.visibility}", user_id=auth.user.id, target_type="project", target_id=project.id)
    await db.commit()
    return _project_out(project, auth.user)


@router.delete("/projects/{project_id}", status_code=204)
async def remove_project(project_id: int, auth: CurrentAuth, db: DB) -> None:
    project = await _own_project(db, project_id, auth.user)
    # Only the code index goes; session summaries that mention the project keep existing (project_id -> NULL).
    await db.execute(
        delete(Document).where(Document.project_id == project.id, Document.source_type.in_(["code", "repo_doc"]))
    )
    await db.delete(project)
    await audit(db, "project.delete", user_id=auth.user.id, target_type="project", target_id=project_id)
    await db.commit()


# ---------- imports ----------


class ImportResult(BaseModel):
    filename: str
    session_id: str | None
    title: str | None
    status: Literal["imported", "unchanged", "failed"]
    error: str | None = None


@router.post("/imports/claude-code", response_model=list[ImportResult])
async def import_claude_code(auth: CurrentAuth, db: DB, files: list[UploadFile] = File(...)) -> list[ImportResult]:
    results = []
    for f in files[:200]:
        name = f.filename or "transcript.jsonl"
        try:
            data = await f.read(50 * 1024 * 1024)
            session, changed = await import_transcript(db, auth.user.id, data)
            await db.commit()
            if changed:
                await defer_once(build_episode_task, f"episode:{session.id}", session_id=str(session.id))
            results.append(
                ImportResult(
                    filename=name,
                    session_id=str(session.id),
                    title=session.title,
                    status="imported" if changed else "unchanged",
                )
            )
        except HTTPException as exc:
            await db.rollback()
            results.append(
                ImportResult(filename=name, session_id=None, title=None, status="failed", error=str(exc.detail))
            )
    await audit(
        db,
        "import.claude_code",
        user_id=auth.user.id,
        detail={"files": len(results), "imported": sum(r.status == "imported" for r in results)},
    )
    await db.commit()
    return results
