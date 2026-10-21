"""Kubeconfig validation before storing (design 02 §1).

Exec plugins and auth providers would run arbitrary commands inside the worker, and file paths would read its
disk, so only embedded credentials are accepted. What is stored is a minimal kubeconfig: the selected context,
its cluster and its user, nothing else.
"""

import base64
from typing import Any

import yaml
from fastapi import status

from app.errors import AppError

MAX_BYTES = 64 * 1024
CLUSTER_KEYS = {"server", "certificate-authority-data", "tls-server-name"}
USER_KEYS = {"token", "client-certificate-data", "client-key-data"}
REFUSED_USER_KEYS = {
    "exec": "exec plugins run commands on our server",
    "auth-provider": "auth providers run commands on our server",
    "tokenFile": "token files are paths on your machine; embed the token instead",
    "client-certificate": "certificate files are paths on your machine; use client-certificate-data",
    "client-key": "key files are paths on your machine; use client-key-data",
    "username": "basic auth is not supported; use a service account token",
    "password": "basic auth is not supported; use a service account token",
    "as": "impersonation is not allowed",
    "as-groups": "impersonation is not allowed",
    "as-uid": "impersonation is not allowed",
    "as-user-extra": "impersonation is not allowed",
}
REFUSED_CLUSTER_KEYS = {
    "certificate-authority": "CA files are paths on your machine; use certificate-authority-data",
    "insecure-skip-tls-verify": "TLS verification can't be turned off",
    "proxy-url": "the proxy is set by the server's configuration",
    "extensions": "extensions are not supported",
}


class KubeconfigError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", f"Kubeconfig refused: {message}")


def _named(items: Any, what: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise KubeconfigError(f"`{what}` must be a list")
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise KubeconfigError(f"every entry in `{what}` needs a name")
        body = item.get(what[:-1])
        if not isinstance(body, dict):
            raise KubeconfigError(f"{what[:-1]} {item['name']!r} has no `{what[:-1]}` section")
        out[item["name"]] = body
    return out


def _b64(value: Any, field: str) -> None:
    try:
        base64.b64decode(str(value), validate=True)
    except ValueError:
        raise KubeconfigError(f"`{field}` is not base64") from None


def _check_user(name: str, user: dict[str, Any]) -> None:
    for key, why in REFUSED_USER_KEYS.items():
        if key in user:
            raise KubeconfigError(f"user {name!r} uses `{key}`: {why}")
    if unknown := sorted(set(user) - USER_KEYS):
        raise KubeconfigError(f"user {name!r} has unsupported fields: {', '.join(unknown)}")
    has_cert = "client-certificate-data" in user or "client-key-data" in user
    if has_cert and not ("client-certificate-data" in user and "client-key-data" in user):
        raise KubeconfigError(f"user {name!r} needs both client-certificate-data and client-key-data")
    if not has_cert and not user.get("token"):
        raise KubeconfigError(f"user {name!r} has no token or client certificate")
    for f in ("client-certificate-data", "client-key-data"):
        if f in user:
            _b64(user[f], f)


def _check_cluster(name: str, cluster: dict[str, Any]) -> None:
    for key, why in REFUSED_CLUSTER_KEYS.items():
        if key in cluster:
            raise KubeconfigError(f"cluster {name!r} uses `{key}`: {why}")
    if unknown := sorted(set(cluster) - CLUSTER_KEYS):
        raise KubeconfigError(f"cluster {name!r} has unsupported fields: {', '.join(unknown)}")
    if "certificate-authority-data" in cluster:
        _b64(cluster["certificate-authority-data"], "certificate-authority-data")


def validate_kubeconfig(text: str, base_url: str) -> str:
    """Return the minimal kubeconfig YAML to store, or raise KubeconfigError with the reason."""
    if len(text.encode()) > MAX_BYTES:
        raise KubeconfigError("larger than 64 KB")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        raise KubeconfigError("not valid YAML") from None
    if not isinstance(doc, dict):
        raise KubeconfigError("not a kubeconfig")
    clusters = _named(doc.get("clusters"), "clusters")
    users = _named(doc.get("users"), "users")
    contexts = _named(doc.get("contexts"), "contexts")
    # Every user and cluster in the file is checked, not just the selected one: a refused field anywhere means
    # the file wasn't made for this, and we don't keep it.
    for n, u in users.items():
        _check_user(n, u)
    for n, c in clusters.items():
        _check_cluster(n, c)
    current = doc.get("current-context") or (next(iter(contexts)) if len(contexts) == 1 else None)
    if current not in contexts:
        raise KubeconfigError("set current-context to the context to use")
    ctx = contexts[current]
    cluster_name, user_name = ctx.get("cluster"), ctx.get("user")
    if cluster_name not in clusters or user_name not in users:
        raise KubeconfigError(f"context {current!r} refers to a missing cluster or user")
    server = str(clusters[cluster_name].get("server", "")).rstrip("/")
    if server != base_url.rstrip("/"):
        raise KubeconfigError(f"the cluster server {server or '(none)'} is not this integration's {base_url}")
    namespace = ctx.get("namespace")
    minimal = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": current,
        "clusters": [{"name": cluster_name, "cluster": clusters[cluster_name]}],
        "users": [{"name": user_name, "user": users[user_name]}],
        "contexts": [
            {
                "name": current,
                "context": {
                    "cluster": cluster_name,
                    "user": user_name,
                    **({"namespace": namespace} if namespace else {}),
                },
            }
        ],
    }
    return yaml.safe_dump(minimal, sort_keys=False)


def parse_stored(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(cluster, user) from a kubeconfig written by validate_kubeconfig."""
    doc = yaml.safe_load(text)
    return doc["clusters"][0]["cluster"], doc["users"][0]["user"]
