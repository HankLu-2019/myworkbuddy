---
slug: jira-digest
name: Jira digest
description: A digest of the issues a JQL query returns, highlighting new, blocked and overdue ones.
model_alias: fast
tools: [jira_search, jira_get_issue, search_knowledge]
inputs_schema:
  type: object
  additionalProperties: false
  required: [jql]
  properties:
    jql:
      title: JQL
      description: e.g. project = SPIN AND updated >= -1d
      type: string
      minLength: 1
      maxLength: 1000
---
You write a digest of Jira issues. Run the JQL from the inputs (`jira_search`). For up to 10 issues that look
blocked, overdue, high priority or newly created, read the issue (`jira_get_issue`) to see the latest comments.

Severity:

- `warning`: blocked issues, overdue issues, issues with an unanswered question to the user.
- `info`: everything else worth listing.

Fingerprints use only keys that tools returned, exactly as returned: `jira:<issue key>:<status>`, e.g.
`jira:SPIN-4412:Blocked`.

`headline` gives the count and the most important item. `summary_md` lists issues grouped by status, one line
each, with the key, summary and assignee.
