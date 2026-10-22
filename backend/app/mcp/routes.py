"""MCP servers API (design 08 §F12). Admins register servers and enable read tools one by one, with a reason; any
signed-in user can list the tools they may use. Every change is audited (rule R3); refreshes call the server with
no transaction open (rule R1)."""

import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from app.audit.service import audit
from app.deps import DB, AdminAuth, CurrentAuth, client_ip
from app.enums import McpToolStatus
from app.errors import AppError
from app.integrations.kinds import validate_base_url
from app.mcp import service
from app.models import McpServer, McpTool
from app.secrets.service import encrypt

router = APIRouter(tags=["mcp"])

NAME = r"^[a-z0-9][a-z0-9-]{1,31}$"
HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_SERVERS = 20
Role = Literal["admin", "member"]
ToolStatus = Literal["disabled", "enabled", "changed", "removed"]


class ToolOut(BaseModel):
    id: int
    name: str
    full_name: str
    description: str
    input_schema: dict
    read_only_hint: bool | None
    write_hint: str | None
    status: ToolStatus
    reason: str | None
    enabled_at: datetime | None


class ServerOut(BaseModel):
    id: int
    name: str
    url: str
    auth_header: str | None
    has_auth: bool
    roles: list[Role]
    enabled: bool
    last_refreshed_at: datetime | None
    last_error: str | None
    tools: list[ToolOut]


class ServerIn(BaseModel):
    name: str = Field(pattern=NAME)
    url: str = Field(min_length=8, max_length=512)
    auth_header: str | None = Field(None, max_length=64)
    auth_value: str | None = Field(None, max_length=4096)
    roles: list[Role] = Field(["admin", "member"], min_length=1)

    @field_validator("auth_header")
    @classmethod
    def _header(cls, v: str | None) -> str | None:
        if v and not HEADER.fullmatch(v):
            raise ValueError("a header name: letters, digits and dashes")
        return v or None


class ServerPatch(BaseModel):
    url: str | None = Field(None, min_length=8, max_length=512)
    auth_header: str | None = Field(None, max_length=64)
    auth_value: str | None = Field(None, max_length=4096)  # "" clears it
    roles: list[Role] | None = Field(None, min_length=1)
    enabled: bool | None = None


class ToolDecision(BaseModel):
    enabled: bool
    reason: str = Field("", max_length=500)


class MyTool(BaseModel):
    name: str
    server: str
    description: str


def _tool_out(server: McpServer, t: McpTool) -> ToolOut:
    return ToolOut(id=t.id, name=t.name, full_name=service.display_name(server.name, t.name),
                   description=t.description, input_schema=t.input_schema,
                   read_only_hint=(t.annotations or {}).get("readOnlyHint"), write_hint=t.write_hint,
                   status=t.status, reason=t.reason, enabled_at=t.enabled_at)  # fmt: skip


async def _out(db, server: McpServer) -> ServerOut:
    tools = await db.scalars(select(McpTool).where(McpTool.server_id == server.id).order_by(McpTool.name))
    return ServerOut(id=server.id, name=server.name, url=server.url, auth_header=server.auth_header,
                     has_auth=bool(server.auth_value_enc), roles=sorted(server.roles), enabled=server.enabled,
                     last_refreshed_at=server.last_refreshed_at, last_error=server.last_error,
                     tools=[_tool_out(server, t) for t in tools])  # fmt: skip


def _url(url: str) -> str:
    try:
        return validate_base_url(url, keep_path=True)
    except AppError as exc:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", exc.detail, field="url") from None


async def _server(db, server_id: int) -> McpServer:
    server = await db.get(McpServer, server_id)
    if server is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "MCP server not found")
    return server


@router.get("/api/admin/mcp-servers", response_model=list[ServerOut])
async def list_servers(auth: AdminAuth, db: DB) -> list[ServerOut]:
    return [await _out(db, s) for s in await db.scalars(select(McpServer).order_by(McpServer.name))]


@router.post("/api/admin/mcp-servers", response_model=ServerOut, status_code=201)
async def create_server(body: ServerIn, request: Request, auth: AdminAuth, db: DB) -> ServerOut:
    if await db.scalar(select(McpServer.id).where(McpServer.name == body.name)) is not None:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", f"There's already a server named {body.name}.",
                       field="name")  # fmt: skip
    if (await db.scalar(select(func.count()).select_from(McpServer)) or 0) >= MAX_SERVERS:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", f"At most {MAX_SERVERS} MCP servers.")
    if bool(body.auth_header) != bool(body.auth_value):
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
                       "Give both the header name and its value, or neither.", field="auth_value")  # fmt: skip
    server = McpServer(name=body.name, url=_url(body.url), auth_header=body.auth_header,
                       auth_value_enc=encrypt(body.auth_value) if body.auth_value else None,
                       roles=sorted(set(body.roles)))  # fmt: skip
    db.add(server)
    await db.flush()
    await audit(db, "mcp_server.create", user_id=auth.user.id, target_type="mcp_server", target_id=server.id,
                detail={"name": server.name, "roles": server.roles}, ip=client_ip(request))  # fmt: skip
    server_id, actor = server.id, auth.user.id
    await db.commit()
    await service.refresh(server_id, actor)  # network: nothing open (R1)
    db.expire_all()
    return await _out(db, await _server(db, server_id))


@router.patch("/api/admin/mcp-servers/{server_id}", response_model=ServerOut)
async def update_server(server_id: int, body: ServerPatch, request: Request, auth: AdminAuth, db: DB) -> ServerOut:
    server = await _server(db, server_id)
    changed = sorted(k for k, v in body.model_dump(exclude_none=True).items())
    moved = body.url is not None and _url(body.url) != server.url
    if body.url is not None:
        server.url = _url(body.url)
    if body.auth_header is not None:
        if body.auth_header and not HEADER.fullmatch(body.auth_header):
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", "Not a header name.",
                           field="auth_header")  # fmt: skip
        server.auth_header = body.auth_header or None
    if body.auth_value is not None:
        server.auth_value_enc = encrypt(body.auth_value) if body.auth_value else None
    if body.roles is not None:
        server.roles = sorted(set(body.roles))
    if body.enabled is not None:
        server.enabled = body.enabled
    server.updated_at = datetime.now(UTC)
    await audit(db, "mcp_server.update", user_id=auth.user.id, target_type="mcp_server", target_id=server.id,
                detail={"fields": [c for c in changed if c != "auth_value"] + (["auth_value"] if body.auth_value
                        is not None else [])}, ip=client_ip(request))  # fmt: skip
    actor = auth.user.id
    await db.commit()
    if moved:  # a new address is a new server as far as trust goes: look at its tools again
        await service.refresh(server_id, actor)
        db.expire_all()
    return await _out(db, await _server(db, server_id))


@router.delete("/api/admin/mcp-servers/{server_id}", status_code=204)
async def delete_server(server_id: int, request: Request, auth: AdminAuth, db: DB) -> None:
    server = await _server(db, server_id)
    await db.delete(server)
    await audit(db, "mcp_server.delete", user_id=auth.user.id, target_type="mcp_server", target_id=server_id,
                ip=client_ip(request))  # fmt: skip
    await db.commit()


@router.post("/api/admin/mcp-servers/{server_id}/refresh", response_model=ServerOut)
async def refresh_server(server_id: int, auth: AdminAuth, db: DB) -> ServerOut:
    await _server(db, server_id)
    actor = auth.user.id
    await db.commit()  # network next (R1)
    await service.refresh(server_id, actor)
    db.expire_all()
    return await _out(db, await _server(db, server_id))


@router.put("/api/admin/mcp-tools/{tool_id}", response_model=ToolOut)
async def decide_tool(tool_id: int, body: ToolDecision, request: Request, auth: AdminAuth, db: DB) -> ToolOut:
    tool = await db.get(McpTool, tool_id)
    if tool is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Tool not found")
    server = await _server(db, tool.server_id)
    if body.enabled:
        if tool.write_hint:
            raise AppError(status.HTTP_409_CONFLICT, "conflict", f"{tool.name} looks like a write tool "
                           f"({tool.write_hint}) and can't be enabled: only read tools for now.")  # fmt: skip
        if tool.status == McpToolStatus.removed:
            raise AppError(status.HTTP_409_CONFLICT, "conflict", "The server no longer lists this tool.")
        if len(body.reason.strip()) < 5:
            raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error",
                           "Say why this tool is needed and why it only reads.", field="reason")  # fmt: skip
        tool.status, tool.reason = McpToolStatus.enabled, body.reason.strip()
        tool.enabled_by, tool.enabled_at = auth.user.id, datetime.now(UTC)
    elif tool.status in (McpToolStatus.enabled, McpToolStatus.changed):
        tool.status = McpToolStatus.disabled
    tool.updated_at = datetime.now(UTC)
    await audit(db, "mcp_tool.enable" if body.enabled else "mcp_tool.disable", user_id=auth.user.id,
                target_type="mcp_tool", target_id=tool.id,
                detail={"tool": service.display_name(server.name, tool.name), "reason": body.reason.strip()[:500],
                        "definition_hash": tool.definition_hash}, ip=client_ip(request))  # fmt: skip
    out = _tool_out(server, tool)
    await db.commit()
    return out


@router.get("/api/mcp-tools", response_model=list[MyTool])
async def my_tools(auth: CurrentAuth, db: DB) -> list[MyTool]:
    """The MCP tools chat may use for you: enabled, read-only, on servers open to your role."""
    pairs = await service.usable(db, auth.user)
    return [MyTool(name=service.display_name(s.name, t.name), server=s.name, description=t.description)
            for s, t in pairs]  # fmt: skip
