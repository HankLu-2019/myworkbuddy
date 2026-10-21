"""Jenkins (design 02 §2): job status, failed builds in a folder, build log tails."""

import time
from typing import Any
from urllib.parse import quote

from app.connectors.base import Connection, ConnectorError, call, call_json, tail_lines

MAX_BUILDS = 50
MAX_LOG_LINES = 400
MAX_SINCE_HOURS = 72


def job_url(path: str) -> str:
    """'folder/sub/job' -> '/job/folder/job/sub/job/job'."""
    parts = [p for p in path.strip().strip("/").split("/") if p and p != "job"]
    if not parts or any(p in ("..", ".") for p in parts):
        raise ConnectorError("invalid_args", "job must be a path like team/service/main")
    return "".join(f"/job/{quote(p, safe='')}" for p in parts)


async def job_status(conn: Connection, job: str) -> dict[str, Any]:
    tree = "name,url,lastBuild[number,result,timestamp,url,building,culprits[fullName]]"
    doc = await call_json(conn, "GET", f"{job_url(job)}/api/json", params={"tree": tree})
    last = doc.get("lastBuild") or {}
    return {
        "job": doc.get("name"),
        "last_build": last.get("number"),
        "result": "RUNNING" if last.get("building") else last.get("result"),
        "timestamp": last.get("timestamp"),
        "culprits": [c.get("fullName") for c in last.get("culprits") or []],
        "url": last.get("url"),
    }


async def failed_builds(conn: Connection, folder: str, since_hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(since_hours, MAX_SINCE_HOURS))
    cutoff = (time.time() - hours * 3600) * 1000
    tree = "jobs[name,url,lastCompletedBuild[number,result,timestamp,url]]"
    doc = await call_json(conn, "GET", f"{job_url(folder)}/api/json", params={"tree": tree})
    failed = []
    for j in doc.get("jobs") or []:
        b = j.get("lastCompletedBuild") or {}
        if b.get("result") in ("FAILURE", "UNSTABLE") and (b.get("timestamp") or 0) >= cutoff:
            failed.append(
                {"job": j.get("name"), "build": b.get("number"), "result": b.get("result"), "url": b.get("url")}
            )
    return {"since_hours": hours, "builds": failed[:MAX_BUILDS], "truncated": len(failed) > MAX_BUILDS}


async def build_log_tail(conn: Connection, job: str, build: int, lines: int = 200) -> dict[str, Any]:
    n = max(1, min(lines, MAX_LOG_LINES))
    body = await call(conn, "GET", f"{job_url(job)}/{int(build)}/consoleText", tail=n * 400)
    return {"build": build, "lines": n, "log": tail_lines(body, n)}
