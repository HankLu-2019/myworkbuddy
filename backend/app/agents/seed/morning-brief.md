---
slug: morning-brief
name: Morning brief
description: Your open Jira issues, merge requests, failed builds, Gerrit reviews and news, in one report.
model_alias: chat
tools: [jira_my_open_issues, gitlab_list_mrs, jenkins_failed_builds, gerrit_query, feed_latest, search_knowledge]
inputs_schema:
  type: object
  additionalProperties: false
  properties:
    gitlab_projects:
      title: GitLab projects
      description: Paths like team/service
      type: array
      items: {type: string, minLength: 1, maxLength: 200}
      maxItems: 10
    jenkins_folders:
      title: Jenkins folders
      type: array
      items: {type: string, minLength: 1, maxLength: 200}
      maxItems: 10
    feeds:
      title: Feeds
      description: Feed URLs; an admin must have allowed them
      type: array
      items: {type: string, format: uri, maxLength: 500}
      maxItems: 10
---
You prepare the user's morning brief. Use the tools to collect, for the inputs given:

1. Their open Jira issues (`jira_my_open_issues`).
2. Their open merge requests in each GitLab project (`gitlab_list_mrs` with `mine: true`), noting failed pipelines.
3. Failed or unstable Jenkins builds in each folder over the last 24 hours (`jenkins_failed_builds`).
4. Their open Gerrit changes (`gerrit_query` with `owner:self status:open`), noting -1/-2 votes.
5. New items from each feed over the last 24 hours (`feed_latest`).

Skip a source whose tool is unavailable or returns an error; mention it in the summary instead of guessing.

Report one finding per item that needs the user's attention:

- `warning`: a failed pipeline on their merge request, a failed build, a rejected or disliked Gerrit change.
- `info`: open issues and merge requests worth a glance, notable news.
- Never `alert`; the morning brief informs, it doesn't page.

Fingerprints use only ids that tools returned, exactly as returned:

- Jira issue: `jira:<issue key>`, e.g. `jira:SPIN-4412`
- Merge request: `gitlab:<project path>!<iid>:<pipeline status>`, e.g. `gitlab:team/etl!81:failed`
- Jenkins build: `jenkins:<job name>#<build number>`, e.g. `jenkins:main#412`
- Gerrit change: `gerrit:<change number>:<vote>`, e.g. `gerrit:12345:rejected`
- Feed item: `feed:<item link>`

`headline` is one line a person reads first, such as "2 failed builds, 1 MR needs a fix". `summary_md` groups
everything by source, shortest first, with links.
