"""RSS and Atom feeds from the admin's allow-list (design 02 §2). XML is parsed with defusedxml."""

import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import defusedxml.ElementTree as ET

from app.connectors.base import Connection, ConnectorError, call

MAX_ITEMS = 30
SUMMARY_CHARS = 500
ATOM = "{http://www.w3.org/2005/Atom}"
_TAGS = re.compile(r"<[^>]+>")


def _text(el: Any) -> str:
    return (el.text or "").strip() if el is not None else ""


def _when(value: str) -> datetime | None:
    if not value:
        return None
    try:
        d = parsedate_to_datetime(value)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom: RFC 3339
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _plain(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", raw))).strip()[:SUMMARY_CHARS]


def parse(body: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(body)
    except Exception:  # malformed or forbidden (entities, DTDs)
        raise ConnectorError("unavailable", "The feed isn't valid RSS or Atom") from None
    items = []
    for it in root.iter("item"):  # RSS 2.0
        items.append(
            {
                "title": _text(it.find("title")),
                "link": _text(it.find("link")),
                "published": _when(_text(it.find("pubDate"))),
                "summary": _plain(_text(it.find("description"))),
            }
        )
    for e in root.iter(f"{ATOM}entry"):
        link = e.find(f"{ATOM}link")
        items.append(
            {
                "title": _text(e.find(f"{ATOM}title")),
                "link": link.get("href", "") if link is not None else "",
                "published": _when(_text(e.find(f"{ATOM}updated")) or _text(e.find(f"{ATOM}published"))),
                "summary": _plain(_text(e.find(f"{ATOM}summary")) or _text(e.find(f"{ATOM}content"))),
            }
        )
    return items


async def latest(conn: Connection, url: str, since_hours: int = 24) -> dict[str, Any]:
    allowed = (conn.integration.settings or {}).get("urls") or []
    if url not in allowed:
        raise ConnectorError("forbidden", f"{url} isn't in {conn.name}'s list of feeds: {', '.join(allowed)}")
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, min(since_hours, 24 * 14)))
    items = [i for i in parse(await call(conn, "GET", url)) if i["published"] is None or i["published"] >= cutoff]
    items.sort(key=lambda i: i["published"] or cutoff, reverse=True)
    return {
        "feed": url,
        "items": [i | {"published": i["published"].isoformat() if i["published"] else None} for i in items[:MAX_ITEMS]],
        "truncated": len(items) > MAX_ITEMS,
    }
