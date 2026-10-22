"""GitLab (design 02 §2): merge requests, pipeline status, job log tails."""

from typing import Any, Literal
from urllib.parse import quote

from app.connectors.base import Connection, ConnectorError, call, call_json, tail_lines

MAX_MRS = 50
MAX_LOG_LINES = 400


def _project(path: str) -> str:
    path = path.strip().strip("/")
    if not path or ".." in path:
        raise ConnectorError("invalid_args", "project must be a path like team/service")
    return quote(path, safe="")


async def list_mrs(
    conn: Connection, project: str, state: Literal["opened", "merged", "closed", "all"] = "opened", mine: bool = False
) -> dict[str, Any]:
    params: dict[str, Any] = {"state": state, "per_page": MAX_MRS, "order_by": "updated_at"}
    if mine:
        params["scope"] = "created_by_me"
    mrs = await call_json(conn, "GET", f"/api/v4/projects/{_project(project)}/merge_requests", params=params) or []
    return {
        "project": project.strip().strip("/"),
        "merge_requests": [
            {
                "iid": m.get("iid"),
                "title": m.get("title"),
                "state": m.get("state"),
                "author": (m.get("author") or {}).get("username"),
                "pipeline_status": (m.get("head_pipeline") or m.get("pipeline") or {}).get("status"),
                "updated": m.get("updated_at"),
                "web_url": m.get("web_url"),
            }
            for m in mrs[:MAX_MRS]
        ],
        "truncated": len(mrs) >= MAX_MRS,
    }


async def pipeline_status(conn: Connection, project: str, ref: str) -> dict[str, Any]:
    pid = _project(project)
    pipelines = (
        await call_json(conn, "GET", f"/api/v4/projects/{pid}/pipelines", params={"ref": ref, "per_page": 1}) or []
    )
    if not pipelines:
        raise ConnectorError("not_found", f"No pipelines for {ref} in {project}")
    p = pipelines[0]
    jobs = (
        await call_json(
            conn,
            "GET",
            f"/api/v4/projects/{pid}/pipelines/{p['id']}/jobs",
            params={"scope[]": "failed", "per_page": 50},
        )
        or []
    )
    return {
        "project": project.strip().strip("/"),
        "pipeline_id": p.get("id"),
        "status": p.get("status"),
        "ref": p.get("ref"),
        "sha": p.get("sha"),
        "updated": p.get("updated_at"),
        "failed_jobs": [{"id": j.get("id"), "name": j.get("name"), "stage": j.get("stage")} for j in jobs],
    }


async def job_log_tail(conn: Connection, project: str, job_id: int, lines: int = 200) -> dict[str, Any]:
    n = max(1, min(lines, MAX_LOG_LINES))
    body = await call(conn, "GET", f"/api/v4/projects/{_project(project)}/jobs/{int(job_id)}/trace", tail=n * 400)
    return {"project": project.strip().strip("/"), "job_id": job_id, "lines": n, "log": tail_lines(body, n)}
