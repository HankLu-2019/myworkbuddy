"""CI ops job: create data worth restoring through the real API (run inside the api container)."""

import json
import os
import sys
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://localhost:8000"


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None)
        req.add_header("content-type", "application/json")
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        with self.opener.open(req) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def login(self, username: str, password: str) -> None:
        self.csrf = self.call("POST", "/api/auth/login", {"username": username, "password": password})["csrf_token"]


admin = Client()
admin.login("admin", os.environ["MWB_E2E_ADMIN_PASSWORD"])
created = admin.call("POST", "/api/admin/users", {"username": "drill"})
member = Client()
member.login("drill", created["initial_password"])
member.call("POST", "/api/auth/password", {"current_password": created["initial_password"], "new_password": "drill-password-1"})
gitlab = admin.call("POST", "/api/admin/integrations", {"kind": "gitlab", "name": "GitLab", "base_url": "https://gitlab.example.com"})
member.call("PUT", f"/api/integrations/{gitlab['id']}/credentials/token", {"value": "glpat-drill-token"})
page = member.call("POST", "/api/wiki/pages", {"path": "notes/drill.md", "content": "# Drill page\n\nRestored from backup."})
ready = json.loads(urllib.request.urlopen(BASE + "/api/ready").read())
print(json.dumps({"user": created["id"], "page": page["id"], "ready": ready}))
sys.exit(0 if ready["ready"] else 1)
