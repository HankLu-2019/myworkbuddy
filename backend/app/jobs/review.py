"""Reviewers' comments on a pushed job's merge request or change (design 08 §F2).

`open_comments()` reads the threads still open (GitLab resolvable discussions not resolved, Gerrit threads whose
latest comment is unresolved). `comments_feedback()` renders the chosen ones for the agent, each body wrapped as
untrusted (rule R6): reviewers are people, but their text isn't the user's and may quote anything. `post_reply()`
answers one thread after an approved push (rule R5), and first looks for the same reply so a redelivered task
doesn't post it twice.
"""

import json
from typing import Any
from urllib.parse import quote

from app.connectors.base import Connection, call_json
from app.jobs.ci import change_number
from app.jobs.push import _gitlab_project
from app.llm.untrusted import UNTRUSTED_NOTICE, wrap
from app.models import Job, Project

BODY_CHARS = 2000
MAX_COMMENTS = 50


async def open_comments(job: Job, conn: Connection, project: Project) -> list[dict[str, Any]]:
    """`[{id, reply_to, author, path, line, body, url}]`; `id` names the thread, `reply_to` what a reply answers."""
    push = job.push or {}
    if project.host == "gerrit":
        number = change_number(push)
        if not number:
            return []
        by_path = await call_json(conn, "GET", f"/a/changes/{quote(str(number))}/comments") or {}
        return _gerrit_threads(by_path, push.get("change_url"))[:MAX_COMMENTS]
    iid = push.get("mr_iid")
    if not iid:
        return []
    pid = _gitlab_project(project, conn.integration)
    discussions = await call_json(
        conn, "GET", f"/api/v4/projects/{pid}/merge_requests/{iid}/discussions", params={"per_page": 100}
    ) or []  # fmt: skip
    out = []
    for d in discussions:
        notes = [n for n in d.get("notes") or [] if not n.get("system")]
        if not notes or not any(n.get("resolvable") and not n.get("resolved") for n in notes):
            continue
        first = notes[0]
        pos = first.get("position") or {}
        out.append({
            "id": str(d.get("id")), "reply_to": str(d.get("id")),
            "author": (first.get("author") or {}).get("username") or "reviewer",
            "path": pos.get("new_path") or pos.get("old_path"), "line": pos.get("new_line") or pos.get("old_line"),
            "body": str(first.get("body") or "")[:BODY_CHARS],
            "url": f"{push.get('mr_url')}#note_{first.get('id')}" if push.get("mr_url") else None,
        })  # fmt: skip
    return out[:MAX_COMMENTS]


def _gerrit_threads(by_path: dict[str, list[dict[str, Any]]], change_url: str | None) -> list[dict[str, Any]]:
    out = []
    for path, comments in by_path.items():
        by_id = {c["id"]: c for c in comments if c.get("id")}

        def root(c: dict[str, Any], by_id: dict = by_id) -> dict[str, Any]:
            seen = set()
            while c.get("in_reply_to") in by_id and c["id"] not in seen:
                seen.add(c["id"])
                c = by_id[c["in_reply_to"]]
            return c

        threads: dict[str, list[dict[str, Any]]] = {}
        for c in by_id.values():
            threads.setdefault(root(c)["id"], []).append(c)
        for rid, items in threads.items():
            latest = max(items, key=lambda c: c.get("updated") or "")
            if not latest.get("unresolved"):
                continue
            first = by_id[rid]
            author = first.get("author") or {}
            name = author.get("username") or author.get("name") or "reviewer"
            out.append({
                "id": rid, "reply_to": latest["id"], "author": name,
                "path": None if path == "/PATCHSET_LEVEL" else path, "line": first.get("line"),
                "body": str(first.get("message") or "")[:BODY_CHARS], "url": change_url,
            })  # fmt: skip
    return out


def comments_feedback(comments: list[dict[str, Any]], note: str | None) -> str:
    lines = [
        "# Reviewers' comments on the merge request",
        "",
        "Address each comment below; if you disagree with one, say why in `.mwb/notes.md` instead of changing code. "
        + UNTRUSTED_NOTICE.replace('Tool results marked "untrusted": true', "The JSON blocks below"),
    ]
    for c in comments:
        where = f"{c['path']}:{c['line']}" if c.get("path") and c.get("line") else c.get("path") or "the change"
        block = wrap(f"review:{c['author']}", c["body"], max_chars=BODY_CHARS)
        lines += [
            "",
            f"## {where} ({c['author']})",
            "",
            "```json",
            json.dumps(block, indent=1, ensure_ascii=False),
            "```",
        ]
    if note and note.strip():
        lines += ["", "## From the reviewer", "", note.strip()]
    return "\n".join(lines) + "\n"


async def post_reply(job: Job, conn: Connection, project: Project, comment: dict[str, Any], body: str) -> str:
    """Post one approved reply, or find it already posted. Returns the reply's id."""
    push = job.push or {}
    if project.host == "gerrit":
        number = quote(str(change_number(push)))
        by_path = await call_json(conn, "GET", f"/a/changes/{number}/comments") or {}
        for c in by_path.get(comment.get("path") or "/PATCHSET_LEVEL", []):
            if c.get("in_reply_to") == comment["reply_to"] and (c.get("message") or "").strip() == body.strip():
                return str(c["id"])
        entry = {"in_reply_to": comment["reply_to"], "message": body, "unresolved": False}
        if comment.get("line"):
            entry["line"] = comment["line"]
        review = {"comments": {comment.get("path") or "/PATCHSET_LEVEL": [entry]}}
        await call_json(conn, "POST", f"/a/changes/{number}/revisions/current/review", json=review)
        by_path = await call_json(conn, "GET", f"/a/changes/{number}/comments") or {}
        for c in by_path.get(comment.get("path") or "/PATCHSET_LEVEL", []):
            if c.get("in_reply_to") == comment["reply_to"] and (c.get("message") or "").strip() == body.strip():
                return str(c["id"])
        return ""
    pid = _gitlab_project(project, conn.integration)
    base = f"/api/v4/projects/{pid}/merge_requests/{push['mr_iid']}/discussions/{quote(comment['reply_to'])}"
    discussion = await call_json(conn, "GET", base) or {}
    note = next((n for n in discussion.get("notes") or [] if (n.get("body") or "").strip() == body.strip()), None)
    if note is None:
        note = await call_json(conn, "POST", f"{base}/notes", json={"body": body})
    await call_json(conn, "PUT", base, params={"resolved": "true"})
    return str((note or {}).get("id", ""))
