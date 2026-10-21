"""Kubernetes (design 02 §2): get resources, warning events and pod log tails, within the admin's allow-list.

`secrets` can never be read, whatever the settings say; `configmaps` only when the admin listed them.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from app.connectors.base import Connection, ConnectorError, call, call_json, tail_lines
from app.integrations.kinds import FORBIDDEN_K8S_KINDS

MAX_ITEMS = 100
MAX_LOG_LINES = 400
MAX_EVENT_MINUTES = 180

# kind -> (API prefix, namespaced)
KINDS: dict[str, tuple[str, bool]] = {
    "pods": ("/api/v1", True),
    "services": ("/api/v1", True),
    "events": ("/api/v1", True),
    "configmaps": ("/api/v1", True),
    "nodes": ("/api/v1", False),
    "namespaces": ("/api/v1", False),
    "deployments": ("/apis/apps/v1", True),
    "statefulsets": ("/apis/apps/v1", True),
    "daemonsets": ("/apis/apps/v1", True),
    "replicasets": ("/apis/apps/v1", True),
    "jobs": ("/apis/batch/v1", True),
    "cronjobs": ("/apis/batch/v1", True),
    "ingresses": ("/apis/networking.k8s.io/v1", True),
}


def _check(conn: Connection, kind: str | None, namespace: str | None) -> None:
    settings = conn.integration.settings or {}
    if kind is not None:
        if kind in FORBIDDEN_K8S_KINDS:
            raise ConnectorError("forbidden", f"{kind} can never be read")
        if kind not in KINDS:
            raise ConnectorError("invalid_args", f"Unknown kind {kind!r}; one of {', '.join(sorted(KINDS))}")
        if kind not in (settings.get("allowed_kinds") or []):
            raise ConnectorError("forbidden", f"{conn.name} doesn't allow reading {kind}")
    allowed_ns = settings.get("allowed_namespaces")
    if namespace is not None and allowed_ns is not None and namespace not in allowed_ns:
        raise ConnectorError("forbidden", f"{conn.name} doesn't allow namespace {namespace!r}")


def _pod(p: dict[str, Any]) -> dict[str, Any]:
    status = p.get("status") or {}
    containers = status.get("containerStatuses") or []
    reasons = sorted(
        {
            (c.get("state") or {}).get(s, {}).get("reason")
            for c in containers
            for s in ("waiting", "terminated")
            if (c.get("state") or {}).get(s, {}).get("reason")
        }
        | {
            (c.get("lastState") or {}).get("terminated", {}).get("reason")
            for c in containers
            if (c.get("lastState") or {}).get("terminated", {}).get("reason")
        }
    )
    return {
        "phase": status.get("phase"),
        "ready": f"{sum(1 for c in containers if c.get('ready'))}/{len(containers)}",
        "restarts": sum(int(c.get("restartCount") or 0) for c in containers),
        "reasons": reasons,
        "node": (p.get("spec") or {}).get("nodeName"),
    }


def _workload(o: dict[str, Any]) -> dict[str, Any]:
    spec, status = o.get("spec") or {}, o.get("status") or {}
    return {
        "desired": spec.get("replicas", status.get("desiredNumberScheduled")),
        "ready": status.get("readyReplicas", status.get("numberReady", 0)),
        "available": status.get("availableReplicas", status.get("numberAvailable", 0)),
        "conditions": [c.get("type") for c in status.get("conditions") or [] if c.get("status") == "True"],
    }


def _node(o: dict[str, Any]) -> dict[str, Any]:
    conds = {c.get("type"): c.get("status") for c in (o.get("status") or {}).get("conditions") or []}
    return {
        "ready": conds.get("Ready") == "True",
        "pressure": [k for k, v in conds.items() if k != "Ready" and v == "True"],
        "unschedulable": bool((o.get("spec") or {}).get("unschedulable")),
    }


def summarize(kind: str, o: dict[str, Any]) -> dict[str, Any]:
    meta = o.get("metadata") or {}
    base = {"name": meta.get("name"), "namespace": meta.get("namespace")}
    if kind == "pods":
        return base | _pod(o)
    if kind in ("deployments", "statefulsets", "daemonsets", "replicasets"):
        return base | _workload(o)
    if kind == "nodes":
        return base | _node(o)
    if kind == "jobs":
        s = o.get("status") or {}
        return base | {"active": s.get("active", 0), "succeeded": s.get("succeeded", 0), "failed": s.get("failed", 0)}
    if kind == "cronjobs":
        s = o.get("status") or {}
        return base | {
            "suspended": bool((o.get("spec") or {}).get("suspend")),
            "last_schedule": s.get("lastScheduleTime"),
        }
    if kind == "configmaps":
        return base | {"keys": sorted((o.get("data") or {}).keys())}  # names only, never values
    return base | {"type": (o.get("spec") or {}).get("type")}


async def get(
    conn: Connection, kind: str, namespace: str | None = None, label_selector: str | None = None
) -> dict[str, Any]:
    _check(conn, kind, namespace)
    prefix, namespaced = KINDS[kind]
    if namespaced and namespace:
        path = f"{prefix}/namespaces/{quote(namespace)}/{kind}"
    else:
        if namespaced and (conn.integration.settings or {}).get("allowed_namespaces") is not None:
            raise ConnectorError("invalid_args", f"Pass namespace; {conn.name} allows only some namespaces")
        path = f"{prefix}/{kind}"
    params: dict[str, Any] = {"limit": MAX_ITEMS}
    if label_selector:
        params["labelSelector"] = label_selector
    doc = await call_json(conn, "GET", path, params=params)
    items = (doc or {}).get("items") or []
    return {
        "kind": kind,
        "items": [summarize(kind, o) for o in items[:MAX_ITEMS]],
        "truncated": bool(((doc or {}).get("metadata") or {}).get("continue")),
    }


async def events(
    conn: Connection, namespace: str, since_minutes: int = 60, warnings_only: bool = True
) -> dict[str, Any]:
    _check(conn, "events", namespace)
    minutes = max(1, min(since_minutes, MAX_EVENT_MINUTES))
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    params: dict[str, Any] = {"limit": 500}
    if warnings_only:
        params["fieldSelector"] = "type=Warning"
    doc = await call_json(conn, "GET", f"/api/v1/namespaces/{quote(namespace)}/events", params=params)
    out = []
    for e in (doc or {}).get("items") or []:
        last = e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp")
        try:
            when = datetime.fromisoformat(str(last).replace("Z", "+00:00")) if last else None
        except ValueError:
            when = None
        if when is not None and when < cutoff:
            continue
        obj = e.get("involvedObject") or {}
        out.append(
            {
                "reason": e.get("reason"),
                "object": f"{obj.get('kind')}/{obj.get('name')}",
                "count": e.get("count") or 1,
                "last_seen": last,
                "message": (e.get("message") or "")[:500],
            }
        )
    out.sort(key=lambda x: str(x["last_seen"]), reverse=True)
    return {"since_minutes": minutes, "events": out[:MAX_ITEMS], "truncated": len(out) > MAX_ITEMS}


async def pod_logs(
    conn: Connection, namespace: str, pod: str, container: str | None = None, lines: int = 200, previous: bool = False
) -> dict[str, Any]:
    _check(conn, "pods", namespace)
    n = max(1, min(lines, MAX_LOG_LINES))
    params: dict[str, Any] = {"tailLines": n, "previous": str(previous).lower()}
    if container:
        params["container"] = container
    body = await call(
        conn, "GET", f"/api/v1/namespaces/{quote(namespace)}/pods/{quote(pod)}/log", params=params, tail=n * 400
    )
    return {"pod": pod, "container": container, "previous": previous, "log": tail_lines(body, n)}
