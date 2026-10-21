"""The verify call per integration kind (design 02 §1): proves a credential works without changing anything."""

import base64
import ssl
from dataclasses import dataclass

import httpx2

from app.integrations import http
from app.integrations.kubeconfig import parse_stored
from app.models import Integration


@dataclass
class VerifyResult:
    ok: bool
    message: str
    auth_failed: bool = False  # 401/403: the credential itself is wrong


def _auth_failure(status: int, what: str) -> VerifyResult | None:
    if status in (401, 403):
        return VerifyResult(False, f"{what} rejected the credential ({status})", auth_failed=True)
    return None


async def _get(c: httpx2.AsyncClient, path: str, what: str) -> tuple[VerifyResult | None, object]:
    status, body = await http.request(c, "GET", path)
    if failure := _auth_failure(status, what):
        return failure, None
    if status != 200:
        return VerifyResult(False, f"{what} answered {status} for {path}"), None
    return None, http.parse_json(body)


async def verify(integration: Integration, creds: dict[str, str]) -> VerifyResult:
    """Never raises for remote problems: they come back as a failed VerifyResult with a readable message."""
    kind, name, base = integration.kind, integration.name, integration.base_url
    try:
        if kind == "feed":
            urls = integration.settings.get("urls") or []
            async with http.client(base, ca_pem=integration.ca_bundle) as c:
                status, _ = await http.request(c, "GET", urls[0])
            return VerifyResult(status == 200, "Feed reachable" if status == 200 else f"Feed answered {status}")
        missing = [k for k in _needed(kind) if not creds.get(k)]
        if missing:
            return VerifyResult(False, f"Missing {', '.join(missing)}")
        if kind == "gitlab":
            async with http.client(base, ca_pem=integration.ca_bundle, headers={"PRIVATE-TOKEN": creds["token"]}) as c:
                err, me = await _get(c, "/api/v4/user", name)
            return err or VerifyResult(True, f"Signed in as {_field(me, 'username')}")
        if kind == "jira":
            version = integration.settings.get("api_version", "2")
            headers = {"Authorization": f"Bearer {creds['token']}"}
            async with http.client(base, ca_pem=integration.ca_bundle, headers=headers) as c:
                err, me = await _get(c, f"/rest/api/{version}/myself", name)
            return err or VerifyResult(True, f"Signed in as {_field(me, 'name') or _field(me, 'displayName')}")
        if kind == "gerrit":
            auth = (creds["username"], creds["password"])
            async with http.client(base, ca_pem=integration.ca_bundle, auth=auth) as c:
                err, me = await _get(c, "/a/accounts/self", name)
            return err or VerifyResult(True, f"Signed in as {_field(me, 'username') or creds['username']}")
        if kind == "jenkins":
            auth = (creds["username"], creds["token"])
            async with http.client(base, ca_pem=integration.ca_bundle, auth=auth) as c:
                err, me = await _get(c, "/me/api/json", name)
            return err or VerifyResult(True, f"Signed in as {_field(me, 'id') or creds['username']}")
        if kind == "kubernetes":
            return await _verify_kubernetes(integration, creds["kubeconfig"])
        return VerifyResult(False, f"Unknown integration kind {kind}")
    except http.ResponseTooLarge as exc:
        return VerifyResult(False, f"{name}: {exc}")
    except (ssl.SSLError, httpx2.ConnectError) as exc:
        reason = (
            "TLS verification failed; does the integration need a CA bundle?"
            if "CERTIFICATE" in str(exc).upper()
            else "can't connect"
        )
        return VerifyResult(False, f"{name}: {reason}")
    except (httpx2.TimeoutException, TimeoutError):
        return VerifyResult(False, f"{name} didn't answer in time")
    except httpx2.HTTPError as exc:
        return VerifyResult(False, f"{name}: {type(exc).__name__}")
    except ValueError:
        return VerifyResult(False, f"{name} answered with something that isn't JSON")


def _needed(kind: str) -> list[str]:
    from app.integrations.kinds import CREDENTIALS

    return list(CREDENTIALS.get(kind, {}))


def _field(doc: object, key: str) -> str:
    return str(doc.get(key, "")) if isinstance(doc, dict) else ""


async def _verify_kubernetes(integration: Integration, kubeconfig: str) -> VerifyResult:
    cluster, user = parse_stored(kubeconfig)
    ca = cluster.get("certificate-authority-data")
    ca_pem = base64.b64decode(ca).decode() if ca else integration.ca_bundle
    headers = {"Authorization": f"Bearer {user['token']}"} if user.get("token") else {}
    cert = base64.b64decode(user["client-certificate-data"]) if "client-certificate-data" in user else None
    key = base64.b64decode(user["client-key-data"]) if "client-key-data" in user else None
    namespaces = integration.settings.get("allowed_namespaces") or ["default"]
    async with http.client(integration.base_url, ca_pem=ca_pem, headers=headers, cert_pem=cert, key_pem=key) as c:
        err, version = await _get(c, "/version", integration.name)
        if err:
            return err
        for ns in namespaces:
            body = {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectRulesReview",
                "spec": {"namespace": ns},
            }
            status, _ = await http.request(
                c, "POST", "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews", json=body
            )
            if failure := _auth_failure(status, integration.name):
                return failure
            if status not in (200, 201):
                return VerifyResult(False, f"Couldn't check permissions in namespace {ns} ({status})")
    return VerifyResult(True, f"Connected to Kubernetes {_field(version, 'gitVersion')}".strip())
