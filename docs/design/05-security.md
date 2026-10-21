# 05 — Threat model and security controls

Status: approved baseline, 2026-10-21. Applies to all phases.

## 1. Assets

| Asset | Where | Why it matters |
|---|---|---|
| User passwords, session tokens | `users`, `auth_sessions` (hashes) | Account takeover |
| Per-user tokens (GitLab, Gerrit, Jira, Jenkins, kubeconfig) | `user_credentials` (MultiFernet) | Access to company systems as that user |
| `MWB_SECRET_KEYS`, LiteLLM master key, runner token, AWS credentials | `.env` on the VM | Unlock everything above, or unbounded LLM spend |
| Private knowledge (wiki, documents, episodes, code index, sessions) | Postgres, `/data/wiki` | Confidential work of each user |
| Company source code | ingest cache, job workspaces | IP |
| Push rights | user tokens used by `worker-jobs` | Code reaching shared repositories |
| Audit log | `audit_log` | Accountability |

## 2. Actors

| Actor | Capability |
|---|---|
| Authenticated member | Normal use; may try to read others' private data or escalate |
| Admin | Manages users and integrations; cannot read others' private knowledge through the UI or API |
| Content author (anyone who can write a Jira ticket, a commit, a log line, a public wiki page, a feed item) | Plants text the models will read: **prompt injection** |
| Code in a repository | Runs inside the sandbox during jobs (tests, install scripts) |
| Network attacker on the company LAN | Sniffs or tampers with traffic |
| Host operator | Has root on the VM; trusted |

## 3. Trust boundaries

1. Browser ↔ Caddy (TLS).
2. Caddy ↔ api (trusted network; only Caddy's IP trusted for forwarded headers).
3. api/worker ↔ external systems (user tokens, TLS with company CA).
4. worker ↔ LLM gateway ↔ Bedrock (model sees untrusted text).
5. worker-jobs ↔ runner (bearer token) ↔ sandbox (untrusted code).
6. sandbox ↔ egress proxy (allowlist).

## 4. Threats and controls

| # | Threat | Controls | Phase |
|---|---|---|---|
| T1 | Credential stuffing / password spraying | argon2id; three-key rate limiting; generic errors; audit of failures | H11 |
| T2 | Session theft | TLS; `__Host-` httpOnly Secure SameSite=Lax cookie; server-side sessions; idle + absolute timeout; revoke on password change; active session list | H3, H11 |
| T3 | CSRF | Per-session token header on unsafe methods; SameSite=Lax; JSON-only bodies | done |
| T4 | XSS from LLM or user content | React escaping; Markdown without raw HTML; strict CSP; no `dangerouslySetInnerHTML` (lint rule) | H3, H4 |
| T5 | Data exfiltration through rendered content (images, auto-loading links) | Remote images never rendered; CSP `img-src 'self' data:`; email bodies plain text without copied links | H3, H4, 02 §5 |
| T6 | Cross-user data leakage | Single ACL module; ACL inside SQL for search; 404 for invisible items; leakage tests per endpoint and tool; public sessions hide private citations | done, R2 |
| T7 | Admin overreach | Admins can unpublish but not read or publish others' private items; offboarding transfers only public items | done, H11 |
| T8 | Prompt injection → harmful action | Read-only tools in Phases 1–3; untrusted envelope; external writes only through approval of the exact payload; plan validation is deterministic; push payload built from templates, not model output | R5, R6, 03, 04 |
| T9 | Prompt injection → misleading answers | Citations to sources; provenance shown ("written by the agent", "from Jira"); run-report fingerprints verified against tool output | 02 §3 |
| T10 | Secret leakage to logs, prompts or the sandbox | Secrets decrypted at use; never in argv/URLs/logs; sandbox env allowlist; gitleaks on patches; structlog redaction processor for keys named `*token*`, `*password*`, `*secret*`, `authorization` | H9, 04 |
| T11 | Malicious kubeconfig executing commands | Exec/auth-provider/file references rejected; server URL must match an admin integration | 02 §1 |
| T12 | SSRF via user-supplied URLs | Repo URLs and integration URLs: https only; integration URLs set by admins; repo URL host must match a configured GitLab/Gerrit integration (Phase 2 onward); feeds only from admin allowlist | H9, 02 |
| T13 | Sandbox escape or abuse | gVisor; non-root; read-only rootfs; no caps; pids/memory/CPU limits; internal network; proxy allowlist; no DNS; no credentials; wall-clock limits; orphan sweeper; runner presets only | 04 |
| T14 | Runner compromise = host root | Runner has a minimal API, no inbound from sandboxes or the edge, bearer token, fixed flags, pinned images, small dependency set; it runs as its own container with only the socket mounted | 04 |
| T15 | Unapproved code reaching shared repos | Push only by orchestrator after approval hash check; branch is `mwb/*` (never the base branch); GitLab MRs are Draft; Gerrit uses `refs/for` | 04 |
| T16 | Runaway LLM spend | Per-user and per-job LiteLLM budgets; per-purpose usage limits; schedule minimum interval; auto-disable after failures | H12, 02 |
| T17 | Data loss | Nightly backups with offsite copy, restore drill; git history for wiki; append-only approvals | H14 |
| T18 | Supply chain | Lockfiles (`uv.lock`, `package-lock.json`); images pinned by digest; Dependabot/Renovate PRs reviewed; CI builds what is deployed | H1, H2 |
| T19 | Sensitive data sent to the LLM provider | Company decision O1 (00 §7); Bedrock in an EU region by default; the OpenAI-compatible route is off unless an admin enables it | H12 |

## 5. Security review checklist per PR

The reviewer (human or `/code-review`) checks:

- [ ] New endpoints: auth, CSRF (unsafe methods), ACL, audit, error codes.
- [ ] New tables with user content: `owner_id`, `visibility`, CHECK constraints, leakage tests.
- [ ] No network call inside a transaction (R1).
- [ ] No secret in logs, prompts, argv, URLs or exceptions.
- [ ] Untrusted text wrapped (R6); no new external-write path without an approval (R5).
- [ ] New Markdown rendering uses the shared safe component.
- [ ] Limits on every input (length, count, size) and every external response (size, time).
