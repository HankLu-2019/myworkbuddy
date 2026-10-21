from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.audit.service import audit
from app.chat.agent import SCOPES
from app.config import get_settings
from app.deps import DB, CurrentAuth
from app.errors import DEFAULT_CODES, AppError
from app.integrations.service import credential_rows, integration_for_repo
from app.knowledge.acl import (
    can_change_visibility,
    can_edit,
    can_view,
    ensure_edit,
    ensure_view,
    visible_filter,
)
from app.knowledge.gitrepo import validate_page_path, validate_sha
from app.knowledge.indexer import set_project_visibility, set_visibility
from app.knowledge.ingest.files import MAX_UPLOAD_BYTES, ingest_upload
from app.knowledge.ingest.repos import list_projects, normalize_repo_url, project_name, validate_repo_url
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
from app.models import Chunk, Document, Integration, LibrarianRun, Project, User, UserSecret, WikiPage
from app.tasks.app import (
    build_episode_task,
    cleanup_project_cache,
    defer_ingest,
    defer_once,
    librarian_user_task,
    project_lock,
)

router = APIRouter(prefix="/api", tags=["knowledge"])


class VisibilityIn(BaseModel):
    visibility: Literal["private", "public"]


# ---------- search ----------


class HitOut(BaseModel):
    document_id: int
    title: str
    source_type: Literal["upload", "wiki", "episode", "code", "repo_doc"]
    source_ref: str
    visibility: Literal["private", "public"]
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
    source_type: Literal["upload", "wiki", "episode", "code", "repo_doc"]
    source_ref: str
    visibility: Literal["private", "public"]
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


@router.put("/knowledge/documents/{doc_id}/file", response_model=DocumentOut)
async def replace_document_file(doc_id: int, auth: CurrentAuth, db: DB, file: UploadFile = File(...)) -> DocumentOut:
    """Replace an uploaded document's content; keeps its id, visibility and owner (design 01 §H9)."""
    doc = await _upload_doc(db, doc_id, auth.user)
    ensure_edit(doc, auth.user)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    doc = await ingest_upload(db, doc.owner_id, file.filename or doc.title, data, replace=doc)
    await audit(db, "document.replace", user_id=auth.user.id, target_type="document", target_id=doc.id)
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
    visibility: Literal["private", "public"]
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


class LibrarianChangeOut(BaseModel):
    sha: str
    date: str
    message: str
    diff: str


@router.get("/wiki/librarian/changes")
async def librarian_changes(auth: CurrentAuth, days: int = 3) -> list[LibrarianChangeOut]:
    repo = repo_for("private", auth.user.id)
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 30)))
    commits = [c for c in await repo.log(limit=100, author=LIBRARIAN.email) if datetime.fromisoformat(c.date) >= since]
    return [LibrarianChangeOut(sha=c.sha, date=c.date, message=c.message, diff=await repo.show(c.sha)) for c in commits]


class RevertOut(BaseModel):
    reverted: list[str]


@router.post("/wiki/librarian/changes/{sha}/revert", response_model=RevertOut)
async def revert_librarian_change(sha: str, auth: CurrentAuth, db: DB) -> RevertOut:
    """Undo one librarian commit in the caller's private wiki with a new commit (07-ux §5.5).

    Refuses (409) when a page changed after the librarian's edit, so a revert never drops newer work.
    """
    sha = validate_sha(sha)
    user = auth.user
    repo = repo_for("private", user.id)
    commit = next((c for c in await repo.log(limit=500, author=LIBRARIAN.email) if c.sha.startswith(sha)), None)
    if commit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No librarian change with that id in your wiki")
    parent = await repo.parent(commit.sha)
    reverted: list[str] = []
    # save_page embeds before it takes the repo lock (rule R1) and then checks base_sha under it, so a page
    # edited between the check below and the write still ends in 409 wiki_conflict, never in lost work.
    async with WikiWriter() as w:
        for path in await repo.changed_files(commit.sha):
            after = await repo.file_at(commit.sha, path)
            if repo.read(path) != after:
                raise AppError(
                    status.HTTP_409_CONFLICT,
                    "conflict",
                    f"{path} changed after the librarian's edit; open the page and change it by hand",
                )
            before = await repo.file_at(parent, path) if parent else None
            page = await find_page(db, user.id, "private", path)
            message = f"Revert librarian change {commit.sha[:8]} to {path}"
            if before is None:
                if page is not None:
                    await delete_page(db, page, user, writer=w)
                else:
                    await w.lock(repo)
                    await w.remove(repo, path, author_of(user), message)
            else:
                await save_page(
                    db, owner=user, path=path, content=before, author=author_of(user), message=message, page=page,
                    base_sha=page.head_sha if page else None, writer=w,
                )  # fmt: skip
            reverted.append(path)
        await audit(
            db, "wiki.revert", user_id=user.id, target_type="commit", target_id=commit.sha, detail={"paths": reverted}
        )
        await db.commit()
    return RevertOut(reverted=reverted)


class RejectedPageOut(BaseModel):
    """A rewrite the guardrails held back (design 01 §H8)."""

    path: str
    reason: str
    diff: str = ""


class LibrarianRunOut(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    local_date: date
    status: Literal["ok", "partial", "failed", "skipped"]
    episodes: int
    pages_updated: list[str]
    pages_rejected: list[RejectedPageOut]
    error: str | None


@router.get("/wiki/librarian/runs")
async def librarian_runs(auth: CurrentAuth, db: DB, days: int = 7) -> list[LibrarianRunOut]:
    """The caller's recent librarian runs, newest first, with rejected rewrites (reason + diff)."""
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 30)))
    rows = await db.scalars(
        select(LibrarianRun)
        .where(LibrarianRun.user_id == auth.user.id, LibrarianRun.started_at >= since)
        .order_by(LibrarianRun.started_at.desc())
        .limit(100)
    )
    return [LibrarianRunOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/wiki/librarian/run", status_code=202)
async def run_librarian(auth: CurrentAuth) -> dict[str, bool]:
    return {"queued": await defer_once(librarian_user_task, f"librarian:{auth.user.id}", user_id=auth.user.id)}


# ---------- projects ----------


class ProjectOut(BaseModel):
    id: int
    name: str
    repo_url: str
    host: Literal["gitlab", "gerrit"]
    branch: str | None
    visibility: Literal["private", "public"]
    status: Literal["new", "queued", "running", "ready", "failed"]
    error: str | None
    last_ingested_sha: str | None
    progress: str | None
    owner_id: int
    updated_at: datetime
    mine: bool
    integration_id: int | None = None
    notice: str | None = None  # set on create when the URL was adjusted (e.g. Gerrit /a/)


class ProjectCreate(BaseModel):
    repo_url: str
    # The integration to clone through; without it, the enabled integration of `host` serving the URL's host.
    integration_id: int | None = None
    host: Literal["gitlab", "gerrit"] | None = None
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
        progress=p.progress if p.status == "running" else None,
        owner_id=p.owner_id,
        updated_at=p.updated_at,
        mine=p.owner_id == user.id,
        integration_id=p.integration_id,
    )


async def _own_project(db: DB, project_id: int, user: User) -> Project:
    project = await db.get(Project, project_id)
    ensure_view(project, user)
    ensure_edit(project, user)
    return project


@router.get("/projects", response_model=list[ProjectOut])
async def get_projects(auth: CurrentAuth, db: DB) -> list[ProjectOut]:
    return [_project_out(p, auth.user) for p in await list_projects(db, auth.user)]


class FixHint(BaseModel):
    """What to do about a failed ingest (07-ux §4.6, §5.10)."""

    message: str
    action: str
    href: str


class ProjectDetail(ProjectOut):
    files: int
    code_chunks: int
    docs: int
    card_page_id: int | None
    hint: FixHint | None


HOST_LABEL = {"gitlab": "GitLab", "gerrit": "Gerrit"}
HOST_SECRET = {"gitlab": "gitlab_token", "gerrit": "gerrit_password"}


def ingest_hint(project: Project, has_credential: bool) -> FixHint | None:
    host = HOST_LABEL.get(project.host, project.host)
    focus = (
        f"/settings/integrations?focus={project.integration_id}" if project.integration_id else "/settings/integrations"
    )
    if not has_credential:
        return FixHint(message=f"Add your {host} token, then re-index.", action="Add token", href=focus)
    if project.status != "failed":
        return None
    error = (project.error or "").lower()
    if any(s in error for s in ("authentication failed", "401", "403", "could not read username", "can't be read")):
        return FixHint(
            message=f"Your {host} token was rejected. Update it, then re-index.", action="Update token", href=focus
        )
    if any(s in error for s in ("not found", "404", "does not exist")):
        return FixHint(
            message="Check the repository URL and that your token can read it.",
            action="Update token",
            href=focus,
        )
    return None


@router.get("/projects/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: int, auth: CurrentAuth, db: DB) -> ProjectDetail:
    project = await db.get(Project, project_id)
    ensure_view(project, auth.user)
    counts = dict(
        (
            await db.execute(
                select(Document.source_type, func.count(Document.id))
                .where(Document.project_id == project.id)
                .group_by(Document.source_type)
            )
        ).all()
    )
    chunks = await db.scalar(
        select(func.count(Chunk.id))
        .join(Document, Document.id == Chunk.document_id)
        .where(Document.project_id == project.id, Document.source_type == "code")
    )
    card = None
    if project.card_page_id and (page := await db.get(WikiPage, project.card_page_id)) and can_view(page, auth.user):
        card = page.id
    mine = project.owner_id == auth.user.id
    has_credential = True
    if mine and project.integration_id is not None:
        rows = await credential_rows(db, auth.user.id, project.integration_id)
        has_credential = {"gitlab": {"token"}, "gerrit": {"username", "password"}}[project.host] <= set(rows)
    elif mine:
        kind = HOST_SECRET.get(project.host)
        has_credential = bool(
            await db.scalar(select(func.count()).where(UserSecret.user_id == auth.user.id, UserSecret.kind == kind))
        )
    return ProjectDetail(
        **_project_out(project, auth.user).model_dump(),
        files=counts.get("code", 0),
        code_chunks=chunks or 0,
        docs=counts.get("repo_doc", 0),
        card_page_id=card,
        hint=ingest_hint(project, has_credential) if mine else None,
    )


async def _integration_for_new_project(db: DB, body: ProjectCreate) -> Integration:
    """Repository URLs must match an enabled GitLab or Gerrit integration (design 02 §1)."""
    if body.integration_id is not None:
        integration = await db.get(Integration, body.integration_id)
        if integration is None or not integration.enabled or integration.kind not in ("gitlab", "gerrit"):
            raise AppError(404, "not_found", "No such GitLab or Gerrit integration")
        return integration
    if body.host is None:
        raise AppError(422, "validation_error", "Choose the GitLab or Gerrit integration the repository is on")
    integration = await integration_for_repo(db, body.host, validate_repo_url(body.repo_url))
    if integration is None:
        host = urlparse(body.repo_url.strip()).hostname or body.repo_url
        label = HOST_LABEL.get(body.host, body.host)
        raise AppError(
            422,
            "validation_error",
            f"No {label} integration serves {host}. Ask an admin to add it in Admin → Integrations.",
        )
    return integration


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def add_project(body: ProjectCreate, auth: CurrentAuth, db: DB) -> ProjectOut:
    integration = await _integration_for_new_project(db, body)
    host = integration.kind
    url, notice = normalize_repo_url(body.repo_url, host)
    if urlparse(url).hostname != urlparse(integration.base_url).hostname:
        raise AppError(422, "validation_error", f"{url} is not on {integration.name} ({integration.base_url})")
    if await db.scalar(select(Project).where(Project.owner_id == auth.user.id, Project.repo_url == url)):
        raise HTTPException(status.HTTP_409_CONFLICT, "You already added this repository")
    project = Project(
        owner_id=auth.user.id,
        repo_url=url,
        host=host,
        integration_id=integration.id,
        branch=body.branch,
        name=body.name or project_name(url),
        status="queued",
    )
    db.add(project)
    await db.flush()
    await audit(db, "project.add", user_id=auth.user.id, target_type="project", target_id=project.id)
    await db.commit()
    await defer_ingest(project.id)
    return _project_out(project, auth.user).model_copy(update={"notice": notice})


@router.post("/projects/{project_id}/ingest", status_code=202)
async def reingest(project_id: int, auth: CurrentAuth, db: DB, force: bool = False) -> dict[str, bool]:
    project = await _own_project(db, project_id, auth.user)
    queued = await defer_ingest(project.id, force=force)
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
    # Under the project lock, so a running ingest finishes before its checkout is removed.
    await defer_once(
        cleanup_project_cache, f"cache-cleanup:{project_id}", lock=project_lock(project_id), project_id=project_id
    )


# ---------- imports ----------


class ImportResult(BaseModel):
    filename: str
    session_id: str | None
    title: str | None
    status: Literal["imported", "unchanged", "failed"]
    error: str | None = None
    code: str | None = None


@router.post("/imports/claude-code", response_model=list[ImportResult])
async def import_claude_code(auth: CurrentAuth, db: DB, files: list[UploadFile] = File(...)) -> list[ImportResult]:
    user_id = auth.user.id  # a failed file rolls back, which expires auth.user
    results = []
    for f in files[:200]:
        name = f.filename or "transcript.jsonl"
        try:
            limit = get_settings().transcript_max_mb * 1024 * 1024
            data = await f.read(limit + 1)
            if len(data) > limit:
                raise AppError(413, "file_too_large", f"Larger than {get_settings().transcript_max_mb} MB")
            session, changed = await import_transcript(db, user_id, data)
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
            code = getattr(exc, "code", None) or DEFAULT_CODES.get(exc.status_code, "bad_request")
            results.append(
                ImportResult(
                    filename=name, session_id=None, title=None, status="failed", error=str(exc.detail), code=code
                )
            )
    await audit(
        db,
        "import.claude_code",
        user_id=user_id,
        detail={"files": len(results), "imported": sum(r.status == "imported" for r in results)},
    )
    await db.commit()
    return results
