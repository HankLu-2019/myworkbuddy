"""What the agent and the verifier read in `.mwb/` (design 04 §5.2–5.4), and how the agent's stream-json output
becomes job events (§5.3).

TASK.md is the approved plan rendered for the agent with the rules it must follow; the plan's own text came from
the planner and the user (it was approved), the Jira ticket is not repeated. prompt.md points at it, and at
feedback.md on retries. verify.json holds the plan's verification commands.
"""

import json
from dataclasses import dataclass, field
from typing import Any

PER_COMMAND_TIMEOUT_S = 600
TEXT_CHARS = 2000
FEEDBACK_LOG_LINES = 200

RULES = """\
## Rules

- Work only in /workspace. Do not run `git commit` or `git push`; the changes are collected from the working tree.
- Change only the files in the plan's list unless you must; explain any other change in `.mwb/notes.md`.
- Run the verification commands below before you finish, and fix what fails.
- Write a short summary of what you did, and anything the reviewer should know, to `.mwb/notes.md`.
- There is no network except the package mirrors (pip, npm, Go modules) through the proxy.
"""


def task_md(key: str, doc: dict[str, Any]) -> str:
    lines = [f"# {key}: {doc['title']}", "", doc["summary"], "", "## Approach", "", doc["approach"], ""]
    lines += ["## File changes", ""]
    lines += [f"- {c['action']} `{c['path']}`: {c['description']}" for c in doc["changes"]]
    if doc.get("tests"):
        lines += ["", "## Tests to write or update", ""]
        lines += [f"- `{t['name']}` ({t['kind']}): {t['description']}" for t in doc["tests"]]
    if doc.get("ambiguities"):
        lines += ["", "## Assumptions (decided)", ""]
        lines += [f"- {a['question']} → {a['assumption']}" for a in doc["ambiguities"]]
    if doc.get("risks"):
        lines += ["", "## Risks to keep in mind", ""]
        lines += [f"- {r['risk']} (mitigation: {r['mitigation']})" for r in doc["risks"]]
    lines += ["", "## Verification commands", "", "```sh", *doc["verification"], "```", "", RULES]
    return "\n".join(lines)


def prompt_md(*, retry: bool, review_feedback: bool) -> str:
    text = "Carry out the task in .mwb/TASK.md in this repository. Follow its rules."
    if review_feedback:
        text += " The reviewer asked for changes: read .mwb/feedback.md first and address all of it."
    elif retry:
        text += " Your previous attempt failed verification: read .mwb/feedback.md first and fix what it lists."
    return text + "\n"


def verify_json(doc: dict[str, Any], total_timeout_s: int) -> str:
    per = min(PER_COMMAND_TIMEOUT_S, total_timeout_s)
    commands = [{"command": c, "timeout_s": per} for c in doc["verification"]]
    return json.dumps({"commands": commands, "total_timeout_s": total_timeout_s}, indent=1)


def feedback_md(failed_checks: list[dict[str, Any]], failed_commands: list[tuple[dict[str, Any], str]]) -> str:
    """What failed, for the agent's next attempt (04 §5.4 step 4)."""
    parts = ["# Verification failed", ""]
    for c in failed_checks:
        parts.append(f"- Check `{c['name']}`: {c['detail']}")
    for result, log in failed_commands:
        tail = "\n".join(log.splitlines()[-FEEDBACK_LOG_LINES:])
        how = "timed out" if result.get("timed_out") else f"exited {result.get('exit_code')}"
        parts += ["", f"## `{result['command']}` {how}", "", "```", tail, "```"]
    return "\n".join(parts) + "\n"


def review_feedback_md(feedback: str) -> str:
    return f"# The reviewer asked for changes\n\n{feedback.strip()}\n"


# ---------- the agent's stream-json (Claude Code --output-format stream-json --verbose) ----------


def _tool_summary(name: str, args: dict[str, Any]) -> str:
    if name == "Bash":
        return str(args.get("command", ""))[:300]
    for key in ("file_path", "path", "pattern", "url"):
        if key in args:
            return str(args[key])[:300]
    return ""


@dataclass
class AgentOutcome:
    tool_uses: int = 0
    result: dict[str, Any] | None = None
    budget_exceeded: bool = False
    api_error: str | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (type, data) not yet emitted


def parse_line(line: str, out: AgentOutcome) -> None:
    """Fold one stdout line into the outcome; text and tool uses become pending events."""
    if not line.startswith("{"):
        return
    try:
        msg = json.loads(line)
    except ValueError:
        return
    kind = msg.get("type")
    if kind == "assistant":
        for block in (msg.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                text = block["text"]
                if "Budget has been exceeded" in text or "budget_exceeded" in text:
                    out.budget_exceeded = True
                if text.startswith("API Error"):
                    out.api_error = text[:500]
                out.events.append(("agent_text", {"text": text[:TEXT_CHARS], "truncated": len(text) > TEXT_CHARS}))
            elif block.get("type") == "tool_use":
                out.tool_uses += 1
                name = block.get("name", "")
                out.events.append(
                    ("agent_tool", {"tool": name, "summary": _tool_summary(name, block.get("input") or {})})
                )
    elif kind == "result":
        out.result = {k: msg.get(k) for k in ("subtype", "is_error", "num_turns", "total_cost_usd", "result")}
        text = str(msg.get("result") or "")
        if "Budget has been exceeded" in text or "budget_exceeded" in text:
            out.budget_exceeded = True
        if msg.get("is_error") and text.startswith("API Error"):
            out.api_error = text[:500]
