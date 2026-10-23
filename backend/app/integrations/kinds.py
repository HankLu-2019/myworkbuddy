"""What each integration kind needs (design 02 §1): its settings model, credential kinds and help text."""

import re
from typing import Any, Literal
from urllib.parse import urlparse

from cryptography import x509
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import get_settings
from app.errors import AppError

# Resource kinds the Phase 2 k8s tools may read; `secrets` is never allowed (02 §2).
DEFAULT_K8S_KINDS = [
    "pods",
    "deployments",
    "statefulsets",
    "daemonsets",
    "jobs",
    "cronjobs",
    "nodes",
    "events",
    "services",
    "ingresses",
]  # fmt: skip  (design 02 §2)
FORBIDDEN_K8S_KINDS = {"secrets", "serviceaccounts/token"}
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
MAX_CA_BYTES = 64 * 1024


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JiraSettings(_Strict):
    api_version: Literal["2", "3"] = "2"


class NoSettings(_Strict):
    pass


class KubernetesSettings(_Strict):
    allowed_kinds: list[str] = Field(default_factory=lambda: list(DEFAULT_K8S_KINDS), min_length=1, max_length=50)
    allowed_namespaces: list[str] | None = Field(default=None, max_length=100)

    @field_validator("allowed_kinds")
    @classmethod
    def _kinds(cls, v: list[str]) -> list[str]:
        if bad := sorted(set(v) & FORBIDDEN_K8S_KINDS):
            raise ValueError(f"{', '.join(bad)} can never be read by MyWorkBuddy")
        return sorted(set(v))

    @field_validator("allowed_namespaces")
    @classmethod
    def _namespaces(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if bad := [n for n in v if not _DNS_LABEL.match(n)]:
            raise ValueError(f"not a namespace name: {', '.join(bad)}")
        return sorted(set(v))


class FeedSettings(_Strict):
    urls: list[str] = Field(min_length=1, max_length=20)

    @field_validator("urls")
    @classmethod
    def _urls(cls, v: list[str]) -> list[str]:
        return [validate_base_url(u, keep_path=True) for u in v]


SETTINGS: dict[str, type[BaseModel]] = {
    "jira": JiraSettings,
    "gitlab": NoSettings,
    "gerrit": NoSettings,
    "jenkins": NoSettings,
    "kubernetes": KubernetesSettings,
    "feed": FeedSettings,
}

# kind -> {credential kind: label}. Order is the order fields are shown.
CREDENTIALS: dict[str, dict[str, str]] = {
    "jira": {"token": "Personal access token"},
    "gitlab": {"token": "Personal access token"},
    "gerrit": {"username": "Username", "password": "HTTP password"},
    "jenkins": {"username": "Username", "token": "API token"},
    "kubernetes": {"kubeconfig": "Kubeconfig"},
    "feed": {},
}

HELP: dict[str, str] = {
    "jira": "Jira → Profile → Personal Access Tokens. Read access is enough in Phase 2.",
    "gitlab": "GitLab → Preferences → Access tokens, with scopes read_api and read_repository.",
    "gerrit": "Gerrit → Settings → HTTP Credentials → Generate new password.",
    "jenkins": "Jenkins → your name → Configure → API Token → Add new token.",
    "kubernetes": (
        "A kubeconfig for a read-only service account, with a token or client certificate embedded. "
        "exec plugins, auth providers and file paths are refused."
    ),
    "feed": "No credentials needed.",
}


def _invalid(message: str) -> AppError:
    return AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", message)


def validate_base_url(url: str, *, keep_path: bool = False) -> str:
    """https only (http when MWB_ALLOW_HTTP_INTEGRATIONS), no credentials, query or fragment; no trailing slash."""
    parsed = urlparse(url.strip())
    s = get_settings()
    http_ok = s.allow_http_integrations or s.http_allowed(parsed.hostname)
    if not parsed.hostname or parsed.scheme not in ({"https", "http"} if http_ok else {"https"}):
        if parsed.scheme == "http" and parsed.hostname:
            raise _invalid(f"{parsed.hostname} isn't allowed over plain http. Use https, or ask the server's admin to "
                           "add the host to MWB_HTTP_INTERNAL_HOSTS.")  # fmt: skip
        raise _invalid(f"{url!r} must be an https URL, e.g. https://gitlab.company.example or https://git.local:8443")
    if parsed.username or parsed.password:
        raise _invalid("Don't put credentials in the URL; users add their own in Settings")
    if parsed.fragment or (parsed.query and not keep_path):
        raise _invalid(f"{url!r} must not have a query or fragment")
    return url.strip().rstrip("/")


def validate_settings(kind: str, settings: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return SETTINGS[kind].model_validate(settings or {}).model_dump()
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "settings"
        raise _invalid(f"settings.{where}: {first.get('msg', 'invalid')}") from None


def validate_ca_bundle(pem: str | None) -> str | None:
    """One or more PEM certificates, or nothing."""
    if pem is None or not pem.strip():
        return None
    if len(pem.encode()) > MAX_CA_BYTES:
        raise _invalid("The CA bundle is larger than 64 KB")
    try:
        certs = x509.load_pem_x509_certificates(pem.encode())
    except ValueError:
        certs = []
    if not certs:
        raise _invalid("The CA bundle must contain PEM certificates (-----BEGIN CERTIFICATE-----)")
    return pem.strip() + "\n"
