"""Jira (design 02 §2): search, one issue, my open issues. Server/Data Center API v2 or Cloud v3."""

import re
from typing import Any
from urllib.parse import quote

from app.connectors.base import Connection, ConnectorError, call_json

KEY = re.compile(r"[A-Z][A-Z0-9_]{0,19}-[0-9]{1,9}")
MAX_RESULTS = 50
MAX_COMMENTS = 20
LIST_FIELDS = "summary,status,assignee,updated"
MY_OPEN_JQL = "assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC"


def _api(conn: Connection) -> str:
    return f"/rest/api/{conn.integration.settings.get('api_version', '2')}"


def adf_text(node: Any) -> str:
    """Plain text from Atlassian Document Format (Cloud, API v3); strings (Server, v2) pass through."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        inner = "".join(adf_text(c) for c in node.get("content", []))
        return inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock") else "")
    if isinstance(node, list):
        return "".join(adf_text(c) for c in node)
    return ""


def _person(p: Any) -> str | None:
    return (p or {}).get("displayName") or (p or {}).get("name") if isinstance(p, dict) else None


def _summary(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "status": (f.get("status") or {}).get("name"),
        "assignee": _person(f.get("assignee")),
        "updated": f.get("updated"),
    }


async def search(conn: Connection, jql: str, max_results: int = 20) -> dict[str, Any]:
    if not jql.strip():
        raise ConnectorError("invalid_args", "jql is empty")
    n = max(1, min(max_results, MAX_RESULTS))
    doc = await call_json(
        conn, "GET", f"{_api(conn)}/search", params={"jql": jql, "maxResults": n, "fields": LIST_FIELDS}
    )
    issues = [_summary(i) for i in (doc or {}).get("issues", [])[:n]]
    total = (doc or {}).get("total", len(issues))
    return {"issues": issues, "total": total, "truncated": total > len(issues)}


async def my_open_issues(conn: Connection) -> dict[str, Any]:
    return await search(conn, MY_OPEN_JQL, MAX_RESULTS)


async def get_issue(conn: Connection, key: str, include_comments: bool = True) -> dict[str, Any]:
    if not key or "/" in key:
        raise ConnectorError("invalid_args", "key must be an issue key like PROJ-12")
    fields = f"{LIST_FIELDS},description,issuetype,priority,labels,reporter,created" + (
        ",comment" if include_comments else ""
    )
    doc = await call_json(conn, "GET", f"{_api(conn)}/issue/{quote(key)}", params={"fields": fields})
    f = doc.get("fields") or {}
    out = _summary(doc) | {
        "type": (f.get("issuetype") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "labels": f.get("labels") or [],
        "reporter": _person(f.get("reporter")),
        "created": f.get("created"),
        "description": adf_text(f.get("description")).strip(),
    }
    if include_comments:
        comments = ((f.get("comment") or {}).get("comments") or [])[-MAX_COMMENTS:]
        out["comments"] = [
            {"author": _person(c.get("author")), "created": c.get("created"), "body": adf_text(c.get("body")).strip()}
            for c in comments
        ]
    return out


# ---------- Phase 3 intake (design 03 §2, §7): whole issues for the per-user cache ----------

ISSUE_FIELDS = "summary,status,assignee,updated,description,issuetype,priority,labels,components,issuelinks"
INTAKE_MAX = 100
PAGE = 50


def full_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """An issue as the work-item cache stores it (no attachments, no comments)."""
    f = issue.get("fields") or {}
    links = []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        kind = (link.get("type") or {}).get("outward" if "outwardIssue" in link else "inward")
        if other.get("key"):
            links.append({"type": kind, "key": other["key"]})
    return {
        "key": issue.get("key"),
        "summary": f.get("summary") or "",
        "status": (f.get("status") or {}).get("name") or "",
        "type": (f.get("issuetype") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "updated": f.get("updated"),
        "assignee": _person(f.get("assignee")),
        "labels": f.get("labels") or [],
        "components": [c.get("name") for c in f.get("components") or [] if c.get("name")],
        "links": links,
        "description": adf_text(f.get("description")).strip(),
    }


async def fetch_issues(conn: Connection, jql: str, *, limit: int = INTAKE_MAX, days: int = 30) -> list[dict[str, Any]]:
    """Issues matching `jql` updated in the last `days`, newest first, at most `limit` (paged)."""
    if not jql.strip():
        raise ConnectorError("invalid_args", "jql is empty")
    query = f"({jql}) AND updated >= -{int(days)}d ORDER BY updated DESC"
    out: list[dict[str, Any]] = []
    while len(out) < limit:
        params = {"jql": query, "startAt": len(out), "maxResults": min(PAGE, limit - len(out)), "fields": ISSUE_FIELDS}
        doc = await call_json(conn, "GET", f"{_api(conn)}/search", params=params) or {}
        page = doc.get("issues") or []
        out += [full_issue(i) for i in page]
        if not page or len(out) >= int(doc.get("total", 0)):
            break
    return out[:limit]


async def fetch_issue(conn: Connection, key: str) -> dict[str, Any]:
    if not KEY.fullmatch(key):
        raise ConnectorError("invalid_args", "key must be an issue key like PROJ-12")
    doc = await call_json(conn, "GET", f"{_api(conn)}/issue/{quote(key)}", params={"fields": ISSUE_FIELDS})
    return full_issue(doc or {})
