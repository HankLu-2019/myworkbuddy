"""The structured output of scheduled agents and its server-side checks (design 02 §3 "Run report").

"Alert only when something changed" must not depend on the model's wording, or on ids it made up. After a run:

- `status` is recomputed from the findings (the model's own value is ignored);
- each finding's fingerprint is checked against what the tools returned in this run. A fingerprint is
  `<source>:<id>[:<qualifier>]`, e.g. `k8s:prod/payments-7f9:CrashLoopBackOff`. Its id part is split into
  tokens on `/ # @ ! ,` and whitespace; it is verified when every token of two or more characters occurs in a
  successful tool result of this run (case-insensitive) and at least one token has three or more.
  Unverified findings stay in the report, marked `verified: false`, and are left out of change detection.
"""

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

SEVERITY_ORDER = {"info": 0, "warning": 1, "alert": 2}
_TOKEN_SPLIT = re.compile(r"[/#@!,\s]+")


class Finding(BaseModel):
    fingerprint: str = Field(
        max_length=200,
        description="Stable id built only from ids that tools returned, e.g. k8s:prod/payments-7f9:CrashLoopBackOff",
    )
    severity: Literal["info", "warning", "alert"]
    title: str = Field(max_length=200)
    detail: str = Field(max_length=4000, description="Markdown")
    links: list[HttpUrl] = Field(default=[], max_length=5)


class RunReport(BaseModel):
    schema_version: Literal[1] = 1
    status: Literal["ok", "warning", "alert"] = Field(description="Highest severity among findings, or ok")
    headline: str = Field(max_length=200)
    findings: list[Finding] = Field(max_length=50)
    summary_md: str = Field(max_length=20000)


def report_status(findings: Iterable[Finding]) -> Literal["ok", "warning", "alert"]:
    top = max((SEVERITY_ORDER[f.severity] for f in findings), default=-1)
    return {-1: "ok", 0: "ok", 1: "warning", 2: "alert"}[top]  # type: ignore[return-value]


def fingerprint_id(fingerprint: str) -> str:
    """The id part: between the source and an optional trailing qualifier (one without `/`, so URLs survive)."""
    parts = fingerprint.split(":")
    if len(parts) < 2:
        return fingerprint
    rest = parts[1:]
    if len(rest) >= 2 and "/" not in rest[-1]:
        rest = rest[:-1]
    return ":".join(rest)


def is_verified(fingerprint: str, corpus: str) -> bool:
    tokens = [t for t in _TOKEN_SPLIT.split(fingerprint_id(fingerprint).lower()) if len(t) >= 2]
    return bool(tokens) and any(len(t) >= 3 for t in tokens) and all(t in corpus for t in tokens)


def evidence(results: Iterable[Any]) -> str:
    """The text that fingerprints are checked against: successful tool results only. Error results are left out,
    because their messages can echo the model's own arguments (an invented key would then verify itself)."""
    texts = []
    for result in results:
        if isinstance(result, dict) and "error" in result and set(result) <= {"error", "message"}:
            continue
        if isinstance(result, dict) and result.get("untrusted") is True:
            texts.append(str(result.get("content", "")))
        else:
            texts.append(json.dumps(result, ensure_ascii=False, default=str))
    return "\n".join(texts).lower()


def check_report(report: RunReport, corpus: str) -> tuple[dict[str, Any], list[str]]:
    """The stored report (status recomputed, findings marked) and its sorted verified fingerprints."""
    stored = report.model_dump(mode="json")
    stored["status"] = report_status(report.findings)
    verified: set[str] = set()
    for finding in stored["findings"]:
        finding["verified"] = is_verified(finding["fingerprint"], corpus)
        if finding["verified"]:
            verified.add(finding["fingerprint"])
    return stored, sorted(verified)


def report_markdown(stored: dict[str, Any]) -> str:
    """The run transcript's final message: what a reader of the session sees."""
    lines = [f"**{stored['headline']}** ({stored['status']})", ""]
    for f in stored["findings"]:
        mark = "" if f.get("verified", True) else " _(unverified)_"
        lines.append(f"- **{f['severity']}**: {f['title']}{mark}")
    if stored["findings"]:
        lines.append("")
    lines.append(stored["summary_md"])
    return "\n".join(lines).strip()
