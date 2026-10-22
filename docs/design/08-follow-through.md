# 08 — Phase 5: follow-through and team rollout

Status: proposed, 2026-10-22. Depends on Phase 4 (jobs, push gate, tracking).

Goal: Phase 4 ends at "a merge request is open". Phase 5 carries the work on until the merge request is **merged**, which means answering CI failures and reviewers. It also brings work in from CI failures and feeds what was learned back into planning. Then it adds what a team needs to adopt the tool: chat notifications, an outcome dashboard, single sign-on and extra tool servers.

The tasks are in priority order. **F1–F5** close the loop on a job, **F6–F7** connect jobs to CI failures and the knowledge base, and **F8–F12** prepare a team rollout. Each task can ship on its own.

Invariants carried over from Phase 4:

1. **Approvals bind every external write** (rule R5): a push, a merge request reply, a Jira issue, a Jira comment. Text from CI logs, review comments, the agent or a repository never triggers one by itself.
2. **Text that MyWorkBuddy didn't write and the user didn't type is untrusted** (rule R6): CI logs, review comments, repository convention files, the agent's questions, MCP tool results. It is wrapped with `app/llm/untrusted.wrap()`, and runs that read it get no external-write tools.
3. **No network call with a transaction open** (rule R1): every task below reads, closes, calls out, then writes in a short transaction that re-checks the status it read.

## F1. Pipeline watch and "Fix CI failure"

**Why:** an open merge request whose pipeline fails isn't done, and today nobody tells the user.

**Watching.** `jobs_track` (04 §6) runs every 10 minutes instead of hourly, and for each `pushed` job also reads the pipeline of the pushed commit:

- **GitLab:** `GET /projects/:id/merge_requests/:iid/pipelines` (newest first), keeping the one whose `sha` equals `jobs.push.pushed_sha`. When it failed: `GET /projects/:id/pipelines/:pid/jobs?scope[]=failed`.
- **Gerrit:** `GET /a/changes/<number>?o=LABELS&o=MESSAGES`. `Verified -1` on the current patch set counts as failed. When a CI message links a Jenkins build on a configured Jenkins integration, the failing build is that one. Otherwise the job tab links the message and "Fix CI failure" uses the message text alone.

The result is stored in `jobs.push.pipeline` as `{sha, status: running|passed|failed|canceled, url, failed: [{name, id, stage}], checked_at}`. A pipeline that fails for the pushed sha adds one inbox item per sha: "PROJ-123: the pipeline failed (test:unit)". It shows on the Job tab's Review request card as a status pill with a link.

**Fixing.** `POST /api/jobs/{id}/fix-ci {note?}` is allowed while `pushed` with a failed pipeline:

1. With nothing open: fetch the end of the logs of up to 3 failed jobs (`gitlab.job_log_tail` or `jenkins.build_log_tail`, 200 lines each, 64 KB in total).
2. Build `feedback.md` on the server:
   - a fixed header ("The merge request's pipeline failed. Make it pass without weakening tests.");
   - each job name with its wrapped log tail;
   - the user's note, if any, which isn't wrapped.
3. Continue exactly like "request changes after a push" (04 §6, X6): `pushed → preparing`, review round + 1, a new job key, the workspace rebuilt from the pushed patch plus the feedback.

The client never sends log text; the server fetches it. The 5-round cap applies.

**Not in F1:** fixing automatically without a click. Each fix still ends in the push preview and needs an approval.

**Acceptance:**
- The fake forge gains pipeline and job endpoints.
- A failed pipeline for the pushed sha adds exactly one inbox item. A pipeline for an older sha is ignored.
- Fix CI failure starts a round whose `feedback.md` holds the wrapped log tails. A prompt injection planted in a log ("ignore previous instructions…") stays inside the envelope and never reaches a system prompt.
- A passed pipeline shows as passed. Gerrit `Verified -1` is shown with its message.
- Fix CI failure is refused without a failed pipeline, and after 5 rounds.

*Changed while building F1:*
- Each log goes into `feedback.md` as a fenced JSON block from `untrusted.wrap()` (`source: "ci:<job>"`), under a header that repeats the untrusted notice. Claude Code's own system prompt isn't ours, so the notice travels with the data.
- A Gerrit failure with no matching Jenkins integration uses the CI message itself as the log.
- The Gerrit change number comes from `push.change_number` or the change URL.
- Home gains a "The pipeline for PROJ-123 failed" card (`pipeline_failed`, action "Fix CI failure").
- `jobs_track` runs every 10 minutes.

## F2. Address review comments

**Why:** reviewers' comments are the other half of getting a merge request merged.

**Reading.** `GET /api/jobs/{id}/review-comments` is allowed while `pushed`. It returns the open comments and is fetched with nothing open:

- **GitLab:** `GET /projects/:id/merge_requests/:iid/discussions`. Keep notes that are resolvable and not resolved, skip system notes, and take file and line from `position`.
- **Gerrit:** `GET /a/changes/<number>/comments`. Keep comments with `unresolved: true` on the latest thread entry.

Each comment is `{id, author, path, line, body (≤ 2000 chars), url}`.

**Acting.** The Job tab gains **Address review comments**. It lists the open comments with checkboxes, all ticked, plus a note box. `POST /api/jobs/{id}/request-changes {feedback?, comment_ids: [...]}`:

- The server fetches the comments again and keeps only the ids sent; it never trusts comment text from the client.
- It writes them to `feedback.md` as `path:line — author: body`, each body wrapped, then the user's note.
- It continues as request changes after a push.

**Replying.** The push preview of the next round gains `review_replies: [{comment_id, body}]`. The default body is "Addressed in <short sha>.", editable, and each reply can be dropped. The list is part of the payload and its hash (rule R5).

- After the push, the orchestrator posts each reply once. GitLab: a note on the discussion, then resolve it. Gerrit: `POST /a/changes/<n>/revisions/current/review` with `in_reply_to` and `unresolved: false`.
- Each reply posted is recorded in `jobs.push.replies`, so a redelivered task doesn't post it twice. Before posting, the orchestrator looks for an identical reply on that thread, as X6 does for the Jira comment.

**Acceptance:**
- The fake forge gains discussions and Gerrit comments.
- Only the selected, still-unresolved comments reach `feedback.md`, wrapped.
- Replies are posted only when approved, once each, including after a crash between posting and recording.
- Resolved comments and system notes are never offered.

*Changed while building F2:*
- A Gerrit thread is open when its **latest** comment is unresolved. A reply goes `in_reply_to` that latest comment, and the thread is listed by its first comment.
- The chosen comments are stored in `jobs.push.addressed` with the round they belong to. The next round's push preview offers a reply for each, and approval refuses replies to other comments (422).
- The reply text may use `<commit placeholder>`, which becomes the pushed commit's short sha.
- Replies are posted after the Jira comment, each recorded in `jobs.push.replies_done`. A redelivery skips recorded replies and finds unrecorded ones by their exact text.
- `request-changes` accepts `comment_ids` only while the job is pushed. `feedback` may be empty when comments are chosen.

## F3. The agent can ask

**Why:** a plan can't settle every detail. Today the agent guesses, and a wrong guess costs a whole review round.

**Agent side.** `TASK.md` and the prompt tell the agent what to do if it reaches a decision the plan doesn't settle and that would change the result. It writes `.mwb/question.md` (one question, ≤ 2000 chars, optionally with the options it sees) and ends its turn without guessing. At most `MWB_JOB_MAX_QUESTIONS` (3) per job; past that, the prompt tells it to choose and say so in `notes.md`.

**Pipeline side:**
- After an agent run, the worker reads `.mwb/question.md` through the runner's `.mwb` route. If it exists, the job goes `running → needs_input`. This is a new status, active but not working, which keeps the workspace and the key.
- The patch so far is stored as an artifact, so the user can see what was done before the question.
- An inbox item and an email: "PROJ-123: the agent has a question".

**Answering.** `POST /api/jobs/{id}/answer {answer ≤ 4000}`:
1. Uploads `.mwb/answer.md` (the question and the answer) as an overlay.
2. Deletes `.mwb/question.md` through a new runner route, `DELETE /v1/workspaces/{id}/mwb/{name}` (files under `.mwb/` only).
3. Moves `needs_input → running` without spending an attempt, and defers the job's task.

The next run is a fresh Claude Code session. `prompt.md` says to read `answer.md` first.

**UI.** The Job tab shows the question as Markdown, labelled "Written by the agent", with an answer box and the patch so far. Home "Needs you" and the Board card show "Question from the agent". Cancel works from `needs_input`. `jobs_cleanup` treats `needs_input` like `awaiting_review`: reminder, then discard after `MWB_JOB_REVIEW_DAYS`.

**Data.** A migration adds `needs_input` to the `jobs.status` CHECK and adds `jobs.questions int not null default 0`. `JobStatus`, the API `Literal`, the transitions and `WORK_ITEM_STATE` (`in_progress`) are updated (rule R9).

**Acceptance:**
- A fake-agent behaviour writes a question and the job reaches `needs_input` with an inbox item.
- After the answer, the next run sees `answer.md`, `question.md` is gone, and the attempt number is unchanged.
- The cap is enforced.
- Cancel and the 7-day cleanup work from `needs_input`.
- The question renders as safe Markdown.

*Changed while building F3:*
- The question lives in `jobs.question` (`{text, number, patch_artifact_id, asked_at}`, JSONB) next to `jobs.questions`, both in migration 0016. That migration also recreates `one_active_job_per_item`, because `needs_input` is active.
- `prompt.md` tells the agent how many questions it may still ask, so the cap is in the prompt as well as enforced. A question written past the cap is removed and the run continues, with an event saying so.
- The answer goes in `answer.md` together with the question.
- The Board card is unchanged: the work item stays In progress. Home "Needs you" shows "The agent working on PROJ-123 has a question" (`job_question`, action "Answer"), and it counts in the Board badge.
- The Jobs filter "Waiting for review" becomes "Waiting for you" and includes `needs_input`.

## F4. Repository conventions

**Why:** every team has rules the planner can't infer: the test command, generated folders, paths not to touch.

**Sources,** first found wins, ≤ 20 KB, read at the plan's base commit:
1. `.mwb/rules.md` in the repository.
2. `AGENTS.md` in the repository.
3. `CLAUDE.md` in the repository.
4. A per-repository **Conventions** text the repository's owner edits on the repository page, for repositories that carry no such file. A migration adds `projects.conventions_md`, ≤ 20 KB.

Claude Code already reads `CLAUDE.md` in its working directory. F4 makes the rules reach the **planner** too, and makes one part of them enforceable.

**Use:**
- The planner context gains a "Repository conventions" section. It is wrapped as untrusted, because it is repository content.
- `TASK.md` names the file the rules came from.
- An optional front-matter block makes paths enforceable:

  ```yaml
  ---
  mwb:
    protected: ["migrations/**", "vendor/**"]
    test: "pytest -q"
  ---
  ```

  - `protected` paths become a new **fail** check, `protected_paths` (04 §5.4).
  - Plan validation warns when a planned change touches a protected path.
  - `test`, when the plan's verification commands don't include it, becomes a validation warning that suggests it.

**Acceptance:**
- File precedence and the size cap.
- The planner prompt carries the conventions inside the untrusted envelope.
- A change to a protected path fails the check, and planning such a change warns.
- Only the repository's owner can edit the Conventions text; others get 404/403 per the ACL tests.

*Changed while building F4:*
- A plan's validation runs again on every edit and approval, so the conventions read when the plan was drafted are kept in `plans.conventions` (`{source, protected, test}`) rather than read again. Migration 0017 adds that column and `projects.conventions_md`.
- The job reads the conventions again from its own clone, at the same commit, and freezes `protected` into its policy.
- Changing a protected path needs the acknowledgement `protected_path:<path>`, like a CI change. The acknowledged path then passes the job check.
- `test` becomes a warning whenever the plan's verification doesn't include it. A plan can't have an empty verification list, so "only when empty" would never fire.
- When the conventions come from the repository setting, the job writes them to `.mwb/conventions.md` and `TASK.md` points there. `TASK.md` also lists the protected paths.
- The Conventions editor sits on the repository page. Teammates see the text read-only.

## F5. Retry a failed push

**Why:** today a push that fails after the reviewed patch ends the job (`failed` at stage `pushing`), and the user starts over.

**Change.** A push failure no longer ends the job. It moves `pushing → awaiting_review`, keeping:
- `jobs.push.state` (how far the push got);
- the approval;
- the workspace and the key;
- `push.error = {code, message, at}`.

The Job tab shows "Push failed: <message>" with **Retry push**.

`POST /api/jobs/{id}/retry-push` moves `awaiting_review → approved_push` with no new approval, but only if the latest `job_push` approval still matches the stored payload and patch (the same checks as 04 §6 step 1). Otherwise the user opens the push preview again.

The orchestrator resumes after the last stored step, so a retry never creates a second merge request or comment. Errors behave as follows:
- `credential_invalid`: the retry works once the user has fixed the credential.
- `branch_moved`: the retry is offered with the explanation. If the branch is still ahead, it fails the same way.
- `approval_mismatch`: still ends the job.

**Data.** A transition `pushing → awaiting_review` is added. No migration.

**Acceptance:**
- The forge answers 503 until retries run out; the job is back in review with the error.
- Retry push resumes, and the forge holds one merge request and one comment.
- A retry after the payload changed is refused (409).
- `approval_mismatch` still fails the job.

*Changed while building F5:* an unexpected error during the push (not only a `StepFailed`) also goes back to review, with code `internal`, because the approval and the patch are intact. The failure adds an inbox item ("PROJ-123: the push failed"). Approving a new push payload clears `push.error`. Retry push refuses a stored payload whose `patch_sha256` no longer matches the job's result (409 `plan_changed`).

## F6. CI failure → ticket

**Why:** the `ci-failures` schedule (02 §3) already explains failures. The next step, a ticket and a plan, is manual today.

**Flow:**
1. On a run result (`/runs/:id`) or its inbox alert, each finding from `ci-failures` gets **Create ticket**.
2. The dialog shows a Jira draft, every field editable:
   - project (the user's Jira projects);
   - issue type;
   - summary (the finding's title);
   - description: the finding's detail and links, and the end of the log;
   - labels: `mwb-ci`, plus the user's auto-plan label as a checkbox (03 §7, which drafts the plan automatically);
   - the repository to plan against (guessed from the GitLab project path or Jenkins folder, editable).
3. `POST /api/runs/{run_id}/findings/{fingerprint}/ticket` with the edited draft:
   - The server hashes the draft and records an approval (`subject_type = 'jira_issue'`, rule R5).
   - With nothing open, it creates the issue with `POST /rest/api/2/issue`. This is a write function in the Jira connector used only by this route; it isn't a tool.
   - It then adds the work item (03 §2) with the chosen repository.

**Idempotency.** A table `finding_tickets(owner_id, fingerprint, jira_key, created_at)` (migration) makes the same finding offer **Open PROJ-321** instead of a second ticket. Before creating, the server searches Jira for the label `mwb-fp-<sha1(fingerprint)[:10]>`, which every created issue carries. A crash between creation and the database write then finds the issue instead of duplicating it.

**Acceptance:**
- Preview → approve → one Jira issue and one work item linked to the repository.
- A second click opens the existing issue.
- A crash after the create doesn't duplicate.
- No issue is created without the approval.
- The description is exactly the approved text.

*Changed while building F6:*
- The routes are `GET /api/runs/{id}/ticket-draft?fingerprint=` and `POST /api/runs/{id}/tickets`. Fingerprints contain slashes, so they go in the query and the body.
- The draft lists the user's Jira projects (`GET /rest/api/2/project`) and guesses the project from the user's latest ticket. It guesses the repository from the fingerprint or the finding's links (the `team/service` part of the path).
- The approval binds the issue's fields (`subject_type = 'jira_issue'`, migration 0018 also widens that CHECK). Its subject is a stable UUID from the user and the fingerprint.
- Jira's create is one attempt (`call(..., retries=0)`): a retried POST could make two issues. A failure leaves nothing recorded, and trying again first finds an issue by its label.
- "Draft a plan" starts the planner directly when the work item has a repository. It doesn't add the auto-plan label, which would need intake to notice it.

## F7. Learn from past jobs

**Why:** a team's merged changes are the best examples of how it wants things done.

**Indexing.** When a job reaches `merged` (X7), `jobs_track` defers `index_change`, which creates a knowledge document of the new source type **`change`**, labelled "Change" in the UI. It contains:
- the ticket key and title;
- the plan's summary and approach;
- the list of files changed with their line counts;
- the commit message;
- the review-round feedback the user wrote (not CI logs, not reviewers' text);
- the merge request link.

It does not contain the diff; the repository's code index already has the merged code.

- **Owner:** the job's owner.
- **Visibility:** the repository's visibility. A shared repository gives a shared change; a private one gives a private change. Access goes through `app/knowledge/acl.py` (rule R2), with leakage tests.
- Embedded with `embedder_for`.

**Use.** The planner's context gains "Similar past changes": the top 3 `change` documents for the same repository by hybrid search on the ticket's summary and description, wrapped, citable in `knowledge_refs`. They are also searchable in Search and Chat.

**Data.** A migration adds `change` to the documents' source CHECK and the source-type enum.

**Acceptance:**
- A merged job yields one document with the right owner, visibility and content.
- Re-indexing is idempotent.
- Another user's private change never appears in their search, chat or planner context.
- The planner context includes similar changes from the same repository only.

*Changed while building F7:*
- Feedback typed in later rounds wasn't stored anywhere except the overlay sent to the sandbox. The status event of each round now keeps the user's own words (`user_note`, ≤ 1000 chars): request changes, the note on Fix CI failure, and the note on Address review comments. The change document lists them under "What the reviewer asked for".
- `index_change` runs on the `llm` queue (it embeds), deferred once per job by `jobs_track` when the job is merged.
- The planner's "Similar past changes" are wrapped as untrusted (they carry Jira and planner text) and carry `document_id`, so the plan can cite them.
- Search and chat can filter `scope: "changes"`, and the UI labels the source "Change". Migration 0019 widens the documents source CHECK.

## F8. Slack and Teams notifications

**Why:** the inbox and email exist (02 §5); teams live in chat (ADR-9 deferred this to Phase 5).

**Settings → Notifications** gains channels. A channel is:
- a Slack incoming webhook, or a Teams workflow webhook, stored encrypted as a user secret (rule R7: never logged, never echoed back; the UI shows `hooks.slack.com/…/****`);
- a filter of inbox kinds and minimum severity;
- a **Send test** button.

Hosts are limited to `MWB_NOTIFY_WEBHOOK_HOSTS`, default `hooks.slack.com, *.webhook.office.com, *.logic.azure.com`.

**Sending:**
- An outbox table `outbound_messages` (migration), sent by a `send_messages` task every minute: retries with backoff, `dedupe_key` per inbox item and channel, at most `MWB_MAX_MESSAGES_PER_DAY` (50) per user.
- The message is the inbox item's title, severity and a link back to MyWorkBuddy. The body is left out by default, so ticket and log text doesn't spread to chat; a per-channel "Include details" option adds the first 500 characters.
- A 401, 403 or 404 from the webhook disables the channel and adds one inbox item.

**Acceptance:**
- One inbox item produces one message per matching channel, even when sent twice.
- Disallowed hosts are refused.
- The webhook URL is in no log line or API response.
- A failing webhook is retried, then disabled with an inbox notice.

*Changed while building F8:*
- The webhook URL is encrypted on the channel row (`notify_channels.url_enc`, the same key as user secrets) rather than in `user_credentials`, which belong to admin integrations. The UI shows `url_hint` (host and the last four characters).
- Each channel keeps a cursor (`last_item_id`): `enqueue()` turns inbox items after it into `outbound_messages` and moves it in the same transaction. A new channel, or one turned back on, starts at the newest item, so nothing old is replayed. Migration 0020 creates both tables.
- 410 also disables the channel (Slack answers 410 for a removed webhook); 429, 5xx and transport errors retry after 1, 5, 15 and 60 minutes, then the message is `failed`.
- The daily cap counts in the user's local day, like email, with one inbox notice when it is reached.
- Titles and details can carry Jira and log text, so Slack's `<`, `>` and `&` are escaped and Markdown links are broken for Teams: a message can't mention `@channel` or add a link other than ours.
- httpx and httpx2 log each request's full URL at INFO, which would put the webhook in the logs; their loggers now log warnings only. A test checks the logs during a send.
- A user has at most 10 channels.

## F9. Outcome dashboard

**Why:** to see which tickets the agent handles well and what it costs, before the tool is rolled out more widely (this covers the backlog's "cost dashboard with trends").

**Insights (`/insights`):** a new nav item under Work. Members see their own work; admins can switch to everyone. Filters: period, repository, issue type. It shows:
- jobs by outcome (merged, closed, discarded, failed by code, timed out, over budget);
- attempts and review rounds per job;
- median time from start to review and from push to merge;
- plan revisions before approval;
- AI cost per merged change and per outcome, and cost per week.

Everything is computed in SQL from `jobs`, `plans`, `plan_revisions`, `job_events` and the existing cost records; there are no new tables. API: `GET /api/insights?from=&to=&scope=me|all&project_id=&issue_type=`; `all` is admin-only. Charts follow the design system; every chart has a table view for accessibility.

**Acceptance:**
- Figures match a seeded fixture exactly.
- Members can't read `scope=all` (403).
- Periods use the user's time zone (rule R8).
- Empty states.

## F10. Verification with containers (Sysbox, opt-in)

**Why:** many repositories can only run their tests with a database or other services in containers. ADR-11 deferred this.

**Design:**
- An admin enables it per repository: a migration adds `projects.verify_services bool`.
- The host needs the Sysbox runtime (`sysbox-runc`), checked on the System page.
- A plan's `sandbox` request gains `services: true`. Validation refuses it when the repository isn't enabled.
- The runner gains a fixed preset, `verify-services`:
  - the image `mwb-sandbox-dind` (the sandbox image plus a rootless `dockerd`);
  - runtime `sysbox-runc`;
  - the same proxy (verify profile), plus `MWB_PROXY_REGISTRY_HOSTS` (a registry mirror) so images can be pulled;
  - CPU, memory and disk limits double the plan's.
- The inner Docker reaches the outside only through the proxy. The host's socket is never mounted.

**Acceptance:**
- Security tests 1–3 (04 §9) repeated from inside an inner container. *Manual* on the VM; CI runners can't run Sysbox.
- A plan with `services: true` on a repository that isn't enabled is a validation error.
- `docker compose up` of a Postgres service works from verify on the VM (*manual*).

## F11. Single sign-on (OIDC)

**Why:** a team rollout shouldn't mean another password.

**Design:**
- OpenID Connect authorization code flow with PKCE, configured by `MWB_OIDC_ISSUER`, `MWB_OIDC_CLIENT_ID`, `MWB_OIDC_CLIENT_SECRET` and `MWB_OIDC_SCOPES`, all validated at startup (rule R14).
- The sign-in page gains **Sign in with <provider>**. Local passwords stay for admins as a fallback, and an admin can turn them off for members.
- Matching: a migration adds `users.oidc_sub`, unique. The first login matches by verified email to an existing user and records `sub`; after that, `sub` only.
- `MWB_OIDC_AUTO_CREATE` (default false) creates unknown users as members; otherwise they see "Ask an admin to add you".
- The ID token is checked: signature against the issuer's keys, `iss`, `aud`, `exp`, `nonce`, plus `state` and the PKCE verifier. Deactivated users are refused. Sessions and CSRF work as today. Every login is audited.

**Acceptance (against a fake OIDC provider in the fake gateway):**
- Login works.
- A wrong `state`, `nonce` or signature, an expired token and the wrong audience are each refused.
- Email matching happens once, then only `sub`.
- Auto-create is on or off by setting.
- A deactivated user can't log in.
- Admins keep the local login.

## F12. Admin-registered MCP servers (read tier)

**Why:** extra data sources (an internal wiki, a ticket system, a metrics API) without writing a connector each time. ADR-3 deferred this to Phase 5.

**Design:**
- **Admin → Integrations** gains **MCP servers**: a name, a streamable HTTP URL (host allowlisted by the existing HTTP-integration rules), an optional auth header stored encrypted, and which roles may use it.
- The server's tool list is fetched and stored (migration: `mcp_servers`, `mcp_tools`).
- Each tool starts disabled. An admin may enable a tool only as **read**, with a reason. Tools whose name or annotations suggest writing (`readOnlyHint: false`, or `destructiveHint`) can't be enabled in this phase. Writes through MCP would need per-call approvals, which is Phase 6 at the earliest.
- Enabled tools appear in the tool registry as `mcp.<server>.<tool>`, so agents, schedules and chat can list them. Each call:
  - is wrapped as untrusted (rule R6), size-capped and given a timeout;
  - runs under the server's credential, shared, which is why there are role limits and only read tools;
  - is audited.
- A changed tool list after a refresh disables the changed tools until an admin looks again (a tool definition that changes silently is a known attack).

**Acceptance:**
- Listing and enabling a tool.
- A write-hinted tool can't be enabled.
- A disabled or unlisted tool can't be called.
- Results are wrapped and capped, and a timeout gives a tool error.
- A changed definition disables the tool.
- Roles are enforced.
- Calls are audited.

## Out of scope (Phase 6 backlog)

User-defined agents and skills editor · Spinnaker connector · reranker (Bedrock Cohere Rerank) · RCA evaluation set · wiki export · a network per sandbox job (04 §1 known limit) · MCP write tools with per-call approval · fixing CI failures without a click.
