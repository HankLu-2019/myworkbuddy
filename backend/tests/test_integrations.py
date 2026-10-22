"""Integrations and credentials (design 02 §1, plan S1)."""

import base64
import datetime as dt
import ipaddress
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.integrations import http
from app.integrations.kubeconfig import validate_kubeconfig
from app.knowledge.ingest import repos
from app.models import AuditLog, Integration, Project, UserCredential, UserSecret
from app.secrets.service import decrypt, set_secret

K8S = "https://k8s.example.com:6443"


@pytest.fixture(autouse=True)
def no_network():
    yield
    http.set_transport(None)


def mock(handler) -> None:
    http.set_transport(httpx2.MockTransport(handler))


async def _admin(client_for):
    return await client_for("root", role="admin")


async def _create(admin, **body):
    r = await admin.post("/api/admin/integrations", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- admin CRUD ----------


async def test_admin_manages_integrations_with_validation_and_audit(client_for) -> None:
    admin = await _admin(client_for)
    alice = await client_for("alice")
    assert (await alice.get("/api/admin/integrations")).status_code == 403
    assert (
        await alice.post("/api/admin/integrations", json={"kind": "jira", "name": "J", "base_url": "https://j"})
    ).status_code == 403

    bad = [
        ({"kind": "jira", "name": "Jira", "base_url": "http://jira.example.com"}, "must be an https URL"),
        ({"kind": "jira", "name": "Jira", "base_url": "https://u:p@jira.example.com"}, "Don't put credentials"),
        (
            {"kind": "jira", "name": "Jira", "base_url": "https://jira.example.com", "settings": {"api_version": "9"}},
            "settings.api_version",
        ),
        ({"kind": "gitlab", "name": "G", "base_url": "https://g.example.com", "settings": {"x": 1}}, "settings.x"),
        (
            {"kind": "kubernetes", "name": "K", "base_url": K8S, "settings": {"allowed_kinds": ["pods", "secrets"]}},
            "secrets can never be read",
        ),
        (
            {"kind": "kubernetes", "name": "K", "base_url": K8S, "settings": {"allowed_namespaces": ["Prod!"]}},
            "not a namespace name",
        ),
        (
            {"kind": "gitlab", "name": "G", "base_url": "https://g.example.com", "ca_bundle": "not a cert"},
            "PEM certificates",
        ),
        ({"kind": "feed", "name": "F", "base_url": "https://f.example.com", "settings": {"urls": []}}, "settings.urls"),
    ]
    for body, message in bad:
        r = await admin.post("/api/admin/integrations", json=body)
        assert r.status_code == 422 and message in r.json()["detail"], (body, r.json())

    jira = await _create(admin, kind="jira", name="Jira", base_url="https://jira.example.com/")
    assert jira["base_url"] == "https://jira.example.com" and jira["settings"] == {"api_version": "2"}
    k8s = await _create(
        admin, kind="kubernetes", name="Prod cluster", base_url=K8S, settings={"allowed_namespaces": ["prod"]}
    )
    assert "pods" in k8s["settings"]["allowed_kinds"] and "secrets" not in k8s["settings"]["allowed_kinds"]
    assert (
        await admin.post(
            "/api/admin/integrations", json={"kind": "gitlab", "name": "jira", "base_url": "https://x.example.com"}
        )
    ).status_code == 409

    r = await admin.patch(f"/api/admin/integrations/{jira['id']}", json={"enabled": False, "name": "Old Jira"})
    assert r.json()["enabled"] is False and r.json()["name"] == "Old Jira"
    assert [i["name"] for i in (await alice.get("/api/integrations")).json()] == ["Prod cluster"]  # disabled hidden
    assert (await admin.delete(f"/api/admin/integrations/{jira['id']}")).status_code == 204
    async with get_sessionmaker()() as db:
        actions = list(await db.scalars(select(AuditLog.action).where(AuditLog.action.like("integration.%"))))
    assert sorted(actions) == ["integration.create", "integration.create", "integration.delete", "integration.update"]


async def test_http_integrations_only_when_allowed(client_for, monkeypatch) -> None:
    admin = await _admin(client_for)
    monkeypatch.setattr(get_settings(), "allow_http_integrations", True)
    assert (await _create(admin, kind="jenkins", name="CI", base_url="http://jenkins.local:8080"))[
        "base_url"
    ].startswith("http://")


async def test_integration_with_repositories_cannot_be_deleted(client_for, git_integrations) -> None:
    admin = await _admin(client_for)
    alice = await client_for("alice")
    await alice.post("/api/projects", json={"repo_url": "https://gitlab.example.com/t/etl.git", "host": "gitlab"})
    r = await admin.delete(f"/api/admin/integrations/{git_integrations['gitlab']}")
    assert r.status_code == 409 and "disable it instead" in r.json()["detail"]
    listed = {i["kind"]: i for i in (await admin.get("/api/admin/integrations")).json()}
    assert listed["gitlab"]["projects"] == 1


async def test_creating_an_integration_adopts_phase_1_tokens(client_for, make_user) -> None:
    admin = await _admin(client_for)
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        await set_secret(db, alice.id, "gerrit_username", "alice")
        await set_secret(db, alice.id, "gerrit_password", "pw")
        await set_secret(db, alice.id, "gitlab_token", "glpat-1")
        db.add(Project(owner_id=alice.id, name="app", repo_url="https://review.example.com/a/app", host="gerrit"))
        db.add(Project(owner_id=alice.id, name="other", repo_url="https://elsewhere.example.com/a/x", host="gerrit"))
        await db.commit()
    gerrit = await _create(admin, kind="gerrit", name="Gerrit", base_url="https://review.example.com")
    async with get_sessionmaker()() as db:
        creds = {c.kind: decrypt(c.ciphertext) for c in await db.scalars(select(UserCredential))}
        assert creds == {"username": "alice", "password": "pw"}
        assert [s.kind for s in await db.scalars(select(UserSecret))] == ["gitlab_token"]  # other kinds untouched
        linked = {p.name: p.integration_id for p in await db.scalars(select(Project))}
    assert linked == {"app": gerrit["id"], "other": None}


# ---------- credentials ----------


async def test_credentials_are_write_only_per_user_and_encrypted(client_for, git_integrations) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    gitlab = git_integrations["gitlab"]
    url = f"/api/integrations/{gitlab}/credentials/token"
    assert (await alice.put(url, json={"value": "  glpat-secret  "})).status_code == 204
    listed = (await alice.get("/api/integrations")).json()
    assert "glpat-secret" not in json.dumps(listed)
    [cred] = next(i for i in listed if i["id"] == gitlab)["credentials"]
    assert cred["status"] == "unverified" and cred["label"] == "Personal access token"
    [bobs] = next(i for i in (await bob.get("/api/integrations")).json() if i["id"] == gitlab)["credentials"]
    assert bobs["status"] == "not_set"  # no leakage between users
    async with get_sessionmaker()() as db:
        [row] = list(await db.scalars(select(UserCredential)))
        assert "glpat" not in row.ciphertext and decrypt(row.ciphertext) == "glpat-secret"
    assert (await alice.put(f"/api/integrations/{gitlab}/credentials/password", json={"value": "x"})).status_code == 404
    assert (await alice.put("/api/integrations/999/credentials/token", json={"value": "x"})).status_code == 404
    assert (await alice.delete(url)).status_code == 204
    alice.headers.pop("X-CSRF-Token")
    assert (await alice.put(url, json={"value": "x"})).json()["code"] == "csrf_invalid"


async def test_anonymous_callers_get_nothing(anon) -> None:
    assert (await anon.get("/api/integrations")).status_code == 401
    assert (await anon.post("/api/integrations/1/test")).status_code == 401


async def test_test_updates_status_and_last_error(client_for, git_integrations) -> None:
    alice = await client_for("alice")
    gitlab = git_integrations["gitlab"]
    assert (await alice.post(f"/api/integrations/{gitlab}/test")).status_code == 409  # nothing saved yet
    await alice.put(f"/api/integrations/{gitlab}/credentials/token", json={"value": "glpat-1"})
    seen: list[httpx2.Request] = []

    def ok(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"username": "alice"})

    mock(ok)
    r = (await alice.post(f"/api/integrations/{gitlab}/test")).json()
    assert r == {"ok": True, "status": "ok", "message": "Signed in as alice"}
    assert seen[0].url.path == "/api/v4/user" and seen[0].headers["PRIVATE-TOKEN"] == "glpat-1"

    mock(lambda request: httpx2.Response(401))
    r = (await alice.post(f"/api/integrations/{gitlab}/test")).json()
    assert r["status"] == "invalid" and "rejected the credential (401)" in r["message"]
    [cred] = next(i for i in (await alice.get("/api/integrations")).json() if i["id"] == gitlab)["credentials"]
    assert cred["status"] == "invalid" and "401" in cred["last_error"]

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    mock(down)
    r = (await alice.post(f"/api/integrations/{gitlab}/test")).json()
    assert r["ok"] is False and "can't connect" in r["message"] and r["status"] == "invalid"  # unchanged by outages


async def test_verify_calls_per_kind(client_for) -> None:
    admin = await _admin(client_for)
    calls: dict[str, httpx2.Request] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls[request.url.host] = request
        if request.url.host == "review.example.com":
            return httpx2.Response(200, content=b')]}\'\n{"username": "alice"}')
        if request.url.host == "jira.example.com":
            return httpx2.Response(200, json={"name": "alice"})
        if request.url.host == "ci.example.com":
            return httpx2.Response(200, json={"id": "alice"})
        return httpx2.Response(200, content=b"<rss/>")

    mock(handler)
    jira = await _create(admin, kind="jira", name="Jira", base_url="https://jira.example.com")
    gerrit = await _create(admin, kind="gerrit", name="Gerrit", base_url="https://review.example.com")
    jenkins = await _create(admin, kind="jenkins", name="Jenkins", base_url="https://ci.example.com")
    feed = await _create(
        admin,
        kind="feed",
        name="News",
        base_url="https://news.example.com",
        settings={"urls": ["https://news.example.com/rss"]},
    )
    await admin.put(f"/api/integrations/{jira['id']}/credentials/token", json={"value": "pat"})
    await admin.put(f"/api/integrations/{gerrit['id']}/credentials/username", json={"value": "alice"})
    r = (await admin.post(f"/api/integrations/{gerrit['id']}/test")).json()
    assert r["ok"] is False and "Missing password" in r["message"]
    await admin.put(f"/api/integrations/{gerrit['id']}/credentials/password", json={"value": "pw"})
    await admin.put(f"/api/integrations/{jenkins['id']}/credentials/username", json={"value": "alice"})
    await admin.put(f"/api/integrations/{jenkins['id']}/credentials/token", json={"value": "tok"})
    for i in (jira, gerrit, jenkins, feed):
        assert (await admin.post(f"/api/integrations/{i['id']}/test")).json()["ok"] is True, i["kind"]
    assert calls["jira.example.com"].url.path == "/rest/api/2/myself"
    assert calls["jira.example.com"].headers["Authorization"] == "Bearer pat"
    assert calls["review.example.com"].url.path == "/a/accounts/self"
    assert calls["review.example.com"].headers["Authorization"] == "Basic " + base64.b64encode(b"alice:pw").decode()
    assert calls["ci.example.com"].url.path == "/me/api/json"
    assert calls["news.example.com"].url.path == "/rss"


async def test_oversized_responses_are_refused(client_for, git_integrations) -> None:
    alice = await client_for("alice")
    gitlab = git_integrations["gitlab"]
    await alice.put(f"/api/integrations/{gitlab}/credentials/token", json={"value": "t"})
    mock(lambda request: httpx2.Response(200, content=b"x" * (http.MAX_RESPONSE_BYTES + 10)))
    r = (await alice.post(f"/api/integrations/{gitlab}/test")).json()
    assert r["ok"] is False and "larger than" in r["message"]


# ---------- kubeconfig ----------


def _kubeconfig(user: dict, cluster: dict | None = None, server: str = K8S) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "ro",
            "clusters": [
                {"name": "c", "cluster": {"server": server, **(cluster or {})}},
                {"name": "other", "cluster": {"server": "https://x"}},
            ],
            "users": [{"name": "u", "user": user}],
            "contexts": [
                {"name": "ro", "context": {"cluster": "c", "user": "u", "namespace": "prod"}},
                {"name": "x", "context": {"cluster": "other", "user": "u"}},
            ],
        }
    )


@pytest.mark.parametrize(
    ("user", "cluster", "server", "message"),
    [
        ({"exec": {"command": "aws"}}, None, K8S, "uses `exec`: exec plugins run commands"),
        ({"auth-provider": {"name": "gcp"}}, None, K8S, "uses `auth-provider`"),
        ({"tokenFile": "/var/run/token"}, None, K8S, "uses `tokenFile`"),
        (
            {"client-certificate": "/home/me/c.crt", "client-key": "/home/me/c.key"},
            None,
            K8S,
            "uses `client-certificate`",
        ),
        ({"username": "admin", "password": "x"}, None, K8S, "basic auth is not supported"),
        ({"token": "t"}, {"certificate-authority": "/etc/ca.pem"}, K8S, "uses `certificate-authority`"),
        ({"token": "t"}, {"insecure-skip-tls-verify": True}, K8S, "can't be turned off"),
        ({"token": "t"}, None, "https://evil.example.com", "is not this integration's"),
        ({}, None, K8S, "has no token or client certificate"),
        ({"client-certificate-data": "aGk="}, None, K8S, "needs both"),
    ],
)
def test_kubeconfig_is_refused_with_a_specific_message(user, cluster, server, message) -> None:
    with pytest.raises(Exception) as err:
        validate_kubeconfig(_kubeconfig(user, cluster, server), K8S)
    assert message in err.value.detail and err.value.code == "validation_error"


def test_kubeconfig_refuses_bad_yaml_and_size() -> None:
    for text, message in [("{{", "not valid YAML"), ("- a", "not a kubeconfig"), ("x" * 70_000, "larger than 64 KB")]:
        with pytest.raises(Exception) as err:
            validate_kubeconfig(text, K8S)
        assert message in err.value.detail


def test_only_the_selected_context_is_kept() -> None:
    stored = yaml.safe_load(validate_kubeconfig(_kubeconfig({"token": "t"}), K8S + "/"))
    assert [c["name"] for c in stored["clusters"]] == ["c"] and [c["name"] for c in stored["contexts"]] == ["ro"]
    assert stored["contexts"][0]["context"]["namespace"] == "prod"


async def test_kubernetes_credential_upload_and_verify(client_for) -> None:
    admin = await _admin(client_for)
    k8s = await _create(
        admin, kind="kubernetes", name="Prod", base_url=K8S, settings={"allowed_namespaces": ["prod", "web"]}
    )
    url = f"/api/integrations/{k8s['id']}/credentials/kubeconfig"
    r = await admin.put(url, json={"value": _kubeconfig({"exec": {"command": "sh"}})})
    assert r.status_code == 422 and "Kubeconfig refused" in r.json()["detail"]
    assert (await admin.put(url, json={"value": _kubeconfig({"token": "sa-token"})})).status_code == 204
    reviews: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Authorization"] == "Bearer sa-token"
        if request.url.path == "/version":
            return httpx2.Response(200, json={"gitVersion": "v1.31.2"})
        reviews.append(json.loads(request.content)["spec"]["namespace"])
        return httpx2.Response(201, json={"status": {"resourceRules": []}})

    mock(handler)
    r = (await admin.post(f"/api/integrations/{k8s['id']}/test")).json()
    assert r == {"ok": True, "status": "ok", "message": "Connected to Kubernetes v1.31.2"}
    assert reviews == ["prod", "web"]
    # Moving the integration to another server drops kubeconfigs that name the old one.
    await admin.patch(f"/api/admin/integrations/{k8s['id']}", json={"base_url": "https://k8s2.example.com"})
    [cred] = next(i for i in (await admin.get("/api/integrations")).json() if i["id"] == k8s["id"])["credentials"]
    assert cred["status"] == "not_set"


# ---------- TLS with a company CA ----------


def _self_signed() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MWB test CA")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


@pytest.fixture
def tls_server(tmp_path):
    cert, key = _self_signed()
    (tmp_path / "c.pem").write_bytes(cert)
    (tmp_path / "k.pem").write_bytes(key)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"username": "tls-user"}).encode()
            self.send_response(200 if self.headers.get("PRIVATE-TOKEN") == "t" else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_port}", cert.decode()
    server.shutdown()


async def test_ca_bundle_is_used_for_tls(client_for, tls_server) -> None:
    base, ca = tls_server
    admin = await _admin(client_for)
    plain = await _create(admin, kind="gitlab", name="No CA", base_url=base)
    with_ca = await _create(admin, kind="gitlab", name="With CA", base_url=base + "/", ca_bundle=ca)
    for i in (plain, with_ca):
        await admin.put(f"/api/integrations/{i['id']}/credentials/token", json={"value": "t"})
    r = (await admin.post(f"/api/integrations/{plain['id']}/test")).json()
    assert r["ok"] is False and "TLS verification failed" in r["message"]
    r = (await admin.post(f"/api/integrations/{with_ca['id']}/test")).json()
    assert r == {"ok": True, "status": "ok", "message": "Signed in as tls-user"}
    assert (await admin.get("/api/admin/integrations")).json()[1]["has_ca_bundle"] is True


def test_client_certificate_is_loaded_from_memory() -> None:
    cert, key = _self_signed()
    ctx = http.ssl_context(None, cert, key)
    assert isinstance(ctx, ssl.SSLContext)


# ---------- repositories and git ----------


async def test_repository_urls_must_match_an_integration_host(client_for, git_integrations) -> None:
    alice = await client_for("alice")
    r = await alice.post("/api/projects", json={"repo_url": "https://git.unknown.example/t/x.git", "host": "gitlab"})
    assert r.status_code == 422 and "No GitLab integration serves git.unknown.example" in r.json()["detail"]
    r = await alice.post(
        "/api/projects",
        json={"repo_url": "https://git.unknown.example/t/x.git", "integration_id": git_integrations["gitlab"]},
    )
    assert r.status_code == 422 and "is not on GitLab" in r.json()["detail"]
    r = await alice.post(
        "/api/projects",
        json={"repo_url": "https://gerrit.example.com/team/app", "integration_id": git_integrations["gerrit"]},
    )
    assert r.status_code == 201 and r.json()["integration_id"] == git_integrations["gerrit"]
    assert r.json()["repo_url"] == "https://gerrit.example.com/a/team/app"
    assert (await alice.post("/api/projects", json={"repo_url": "https://gerrit.example.com/x"})).status_code == 422


async def test_git_uses_the_integration_credentials_and_ca(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    cert, _ = _self_signed()
    calls: list[tuple[tuple[str, ...], str | None, object]] = []

    async def fake_git(*args, cwd, auth_header=None, ca_file=None):
        calls.append((args, auth_header, ca_file))
        return ""

    monkeypatch.setattr(repos, "run_git", fake_git)
    async with get_sessionmaker()() as db:
        integration = Integration(
            kind="gitlab", name="GitLab", base_url="https://gitlab.example.com", ca_bundle=cert.decode()
        )
        db.add(integration)
        await db.flush()
        from app.integrations.service import set_credential

        await set_credential(db, alice.id, integration, "token", "glpat-new")
        project = Project(
            owner_id=alice.id,
            name="p",
            repo_url="https://gitlab.example.com/t/p.git",
            host="gitlab",
            integration_id=integration.id,
        )
        db.add(project)
        await db.commit()
        await repos.sync_checkout(db, project)
    args, header, ca_file = calls[0]
    assert header == "Authorization: Basic " + base64.b64encode(b"oauth2:glpat-new").decode()
    assert ca_file is not None and ca_file.read_text() == cert.decode()
    env = repos.git_env(header, ca_file)
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_CONFIG_KEY_1"] == "http.sslCAInfo"


async def test_disabled_integration_stops_ingest(make_user) -> None:
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        integration = Integration(kind="gitlab", name="GitLab", base_url="https://gitlab.example.com", enabled=False)
        db.add(integration)
        await db.flush()
        project = Project(
            owner_id=alice.id,
            name="p",
            repo_url="https://gitlab.example.com/t/p.git",
            host="gitlab",
            integration_id=integration.id,
        )
        db.add(project)
        await db.commit()
        with pytest.raises(Exception) as err:
            await repos.auth_header(db, project)
    assert "is disabled" in err.value.detail


async def test_enum_columns_are_checked_by_the_database(git_integrations) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db import get_engine

    for stmt in [
        "INSERT INTO integrations (kind, name, base_url) VALUES ('slack', 's', 'https://s')",
        "UPDATE user_credentials SET status = 'great'",
    ]:
        with pytest.raises(IntegrityError):
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("INSERT INTO users (username, password_hash) VALUES ('c', 'x') ON CONFLICT DO NOTHING")
                )
                await conn.execute(
                    text(
                        "INSERT INTO user_credentials (user_id, integration_id, kind, ciphertext) "
                        "SELECT id, :i, 'token', 'x' FROM users WHERE username = 'c' ON CONFLICT DO NOTHING"
                    ),
                    {"i": git_integrations["gitlab"]},
                )
                await conn.execute(text(stmt))
