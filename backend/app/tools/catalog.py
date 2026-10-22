"""Every agent tool, registered with its tier, integration and output limits (design 02 §2 "Phase 2 read tools").

Docstrings and type hints are what the model sees as the tool description and argument schema.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic_ai import RunContext

from app.connectors import feed, gerrit, gitlab, jenkins, jira, kubernetes
from app.connectors.base import Connection, connect
from app.schedules.proposals import propose_schedule
from app.tools.knowledge import get_session, search_knowledge
from app.tools.registry import Tier, ToolSpec, register

LIST_CHARS = 20_000
LOG_CHARS = 40_000


async def _run[T](
    ctx: RunContext[Any], kind: str, integration: str | None, fn: Callable[[Connection], Awaitable[T]]
) -> T:
    conn = await connect(ctx.deps.user.id, kind, integration)
    try:
        return await fn(conn)
    finally:
        await conn.close()


# ---------- Jira ----------


async def jira_search(ctx: RunContext[Any], jql: str, max_results: int = 20, integration: str | None = None) -> Any:
    """Search Jira issues with JQL.

    Args:
        jql: a JQL query, e.g. `project = SPIN AND status = "In Progress"`.
        max_results: at most 50.
        integration: the Jira integration's name, only if the user has several.
    """
    return await _run(ctx, "jira", integration, lambda c: jira.search(c, jql, max_results))


async def jira_get_issue(
    ctx: RunContext[Any], key: str, include_comments: bool = True, integration: str | None = None
) -> Any:
    """One Jira issue with its description and the last 20 comments.

    Args:
        key: the issue key, e.g. SPIN-4412.
        include_comments: whether to include comments.
        integration: the Jira integration's name, only if the user has several.
    """
    return await _run(ctx, "jira", integration, lambda c: jira.get_issue(c, key, include_comments))


async def jira_my_open_issues(ctx: RunContext[Any], integration: str | None = None) -> Any:
    """The user's unresolved Jira issues, most recently updated first.

    Args:
        integration: the Jira integration's name, only if the user has several.
    """
    return await _run(ctx, "jira", integration, jira.my_open_issues)


# ---------- GitLab ----------


async def gitlab_list_mrs(
    ctx: RunContext[Any],
    project: str,
    state: Literal["opened", "merged", "closed", "all"] = "opened",
    mine: bool = False,
    integration: str | None = None,
) -> Any:
    """Merge requests of a GitLab project.

    Args:
        project: the project path, e.g. team/payments-api.
        state: which merge requests.
        mine: only merge requests the user created.
        integration: the GitLab integration's name, only if the user has several.
    """
    return await _run(ctx, "gitlab", integration, lambda c: gitlab.list_mrs(c, project, state, mine))


async def gitlab_pipeline_status(ctx: RunContext[Any], project: str, ref: str, integration: str | None = None) -> Any:
    """The latest pipeline for a branch or tag, with its failed jobs.

    Args:
        project: the project path, e.g. team/payments-api.
        ref: a branch or tag, e.g. main.
        integration: the GitLab integration's name, only if the user has several.
    """
    return await _run(ctx, "gitlab", integration, lambda c: gitlab.pipeline_status(c, project, ref))


async def gitlab_job_log_tail(
    ctx: RunContext[Any], project: str, job_id: int, lines: int = 200, integration: str | None = None
) -> Any:
    """The last lines of a GitLab CI job log.

    Args:
        project: the project path.
        job_id: the job id from gitlab_pipeline_status.
        lines: at most 400.
        integration: the GitLab integration's name, only if the user has several.
    """
    return await _run(ctx, "gitlab", integration, lambda c: gitlab.job_log_tail(c, project, job_id, lines))


# ---------- Gerrit ----------


async def gerrit_query(ctx: RunContext[Any], query: str, limit: int = 25, integration: str | None = None) -> Any:
    """Gerrit changes matching a query, with their Verified and Code-Review votes.

    Args:
        query: Gerrit search, e.g. `owner:self status:open`.
        limit: at most 50.
        integration: the Gerrit integration's name, only if the user has several.
    """
    return await _run(ctx, "gerrit", integration, lambda c: gerrit.query(c, query, limit))


# ---------- Jenkins ----------


async def jenkins_job_status(ctx: RunContext[Any], job: str, integration: str | None = None) -> Any:
    """The last build of a Jenkins job.

    Args:
        job: the job path, e.g. team/payments/main.
        integration: the Jenkins integration's name, only if the user has several.
    """
    return await _run(ctx, "jenkins", integration, lambda c: jenkins.job_status(c, job))


async def jenkins_failed_builds(
    ctx: RunContext[Any], folder: str, since_hours: int = 24, integration: str | None = None
) -> Any:
    """Jobs in a Jenkins folder whose last completed build failed or was unstable.

    Args:
        folder: the folder path, e.g. team/payments.
        since_hours: at most 72.
        integration: the Jenkins integration's name, only if the user has several.
    """
    return await _run(ctx, "jenkins", integration, lambda c: jenkins.failed_builds(c, folder, since_hours))


async def jenkins_build_log_tail(
    ctx: RunContext[Any], job: str, build: int, lines: int = 200, integration: str | None = None
) -> Any:
    """The last lines of a Jenkins build's console log.

    Args:
        job: the job path.
        build: the build number.
        lines: at most 400.
        integration: the Jenkins integration's name, only if the user has several.
    """
    return await _run(ctx, "jenkins", integration, lambda c: jenkins.build_log_tail(c, job, build, lines))


# ---------- Kubernetes ----------


async def k8s_get(
    ctx: RunContext[Any],
    kind: str,
    namespace: str | None = None,
    label_selector: str | None = None,
    integration: str | None = None,
) -> Any:
    """Kubernetes resources with a status summary (for pods: phase, ready, restarts, reasons).

    Args:
        kind: pods, deployments, statefulsets, daemonsets, jobs, cronjobs, nodes, events, services or ingresses
            (whatever the admin allowed; never secrets).
        namespace: the namespace; leave out for nodes.
        label_selector: e.g. app=payments.
        integration: the cluster's integration name, only if the user has several.
    """
    return await _run(ctx, "kubernetes", integration, lambda c: kubernetes.get(c, kind, namespace, label_selector))


async def k8s_events(
    ctx: RunContext[Any],
    namespace: str,
    since_minutes: int = 60,
    warnings_only: bool = True,
    integration: str | None = None,
) -> Any:
    """Recent Kubernetes events in a namespace, newest first.

    Args:
        namespace: the namespace.
        since_minutes: at most 180.
        warnings_only: only Warning events.
        integration: the cluster's integration name, only if the user has several.
    """
    return await _run(
        ctx, "kubernetes", integration, lambda c: kubernetes.events(c, namespace, since_minutes, warnings_only)
    )


async def k8s_pod_logs(
    ctx: RunContext[Any],
    namespace: str,
    pod: str,
    container: str | None = None,
    lines: int = 200,
    previous: bool = False,
    integration: str | None = None,
) -> Any:
    """The last lines of a pod's log.

    Args:
        namespace: the namespace.
        pod: the pod name.
        container: needed when the pod has several containers.
        lines: at most 400.
        previous: the previous (crashed) container instance.
        integration: the cluster's integration name, only if the user has several.
    """
    return await _run(
        ctx, "kubernetes", integration, lambda c: kubernetes.pod_logs(c, namespace, pod, container, lines, previous)
    )


# ---------- feeds ----------


async def feed_latest(ctx: RunContext[Any], url: str, since_hours: int = 24, integration: str | None = None) -> Any:
    """Recent items of an RSS or Atom feed from the admin's list.

    Args:
        url: one of the feed URLs the admin configured.
        since_hours: how far back, at most two weeks.
        integration: the feed integration's name; usually not needed, the URL picks it.
    """
    if integration is None:
        integration = await feed.integration_for(url)
    return await _run(ctx, "feed", integration, lambda c: feed.latest(c, url, since_hours))


def _spec(fn, kind: str | None, untrusted: bool, chars: int = LIST_CHARS) -> ToolSpec:
    return register(ToolSpec(fn.__name__, Tier.read, kind, untrusted, chars, fn))


# Internal tools wrap untrusted text per hit themselves.
_spec(search_knowledge, None, False)
_spec(get_session, None, False, 40_000)
_spec(propose_schedule, None, False, 4_000)  # creates nothing; the user's click does (02 §6)
_spec(jira_search, "jira", True)
_spec(jira_get_issue, "jira", True, 30_000)
_spec(jira_my_open_issues, "jira", True)
_spec(gitlab_list_mrs, "gitlab", True)
_spec(gitlab_pipeline_status, "gitlab", False)
_spec(gitlab_job_log_tail, "gitlab", True, LOG_CHARS)
_spec(gerrit_query, "gerrit", True)
_spec(jenkins_job_status, "jenkins", False)
_spec(jenkins_failed_builds, "jenkins", False)
_spec(jenkins_build_log_tail, "jenkins", True, LOG_CHARS)
_spec(k8s_get, "kubernetes", False)
_spec(k8s_events, "kubernetes", True)
_spec(k8s_pod_logs, "kubernetes", True, LOG_CHARS)
_spec(feed_latest, "feed", True)
