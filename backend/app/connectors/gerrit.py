"""Gerrit (design 02 §2): change queries with their votes."""

from typing import Any

from app.connectors.base import Connection, ConnectorError, call_json

MAX_CHANGES = 50


def _vote(label: dict[str, Any] | None) -> str | None:
    if not label:
        return None
    for key in ("rejected", "approved", "disliked", "recommended"):
        if key in label:
            return key
    return None


async def query(conn: Connection, q: str, limit: int = 25) -> dict[str, Any]:
    if not q.strip():
        raise ConnectorError("invalid_args", "query is empty, e.g. owner:self status:open")
    n = max(1, min(limit, MAX_CHANGES))
    changes = await call_json(conn, "GET", "/a/changes/", params={"q": q, "n": n, "o": "LABELS"}) or []
    return {
        "changes": [
            {
                "number": c.get("_number"),
                "project": c.get("project"),
                "subject": c.get("subject"),
                "status": c.get("status"),
                "updated": c.get("updated"),
                "verified": _vote((c.get("labels") or {}).get("Verified")),
                "code_review": _vote((c.get("labels") or {}).get("Code-Review")),
            }
            for c in changes[:n]
        ],
        "truncated": bool(changes and changes[-1].get("_more_changes")),
    }
