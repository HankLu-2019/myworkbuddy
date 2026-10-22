"""PlanDoc: the structured plan the planner writes and the user approves (design 03 §3).

`extra="forbid"` everywhere, so a revision is exactly these fields. `content_hash()` is what an approval binds to:
sha256 of the canonical JSON (sorted keys, no whitespace, UTF-8), stable across key order and formatting.
"""

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileChange(_Strict):
    path: str = Field(max_length=300)
    action: Literal["create", "modify", "delete"]
    description: str = Field(max_length=1000)


class TestItem(_Strict):
    __test__ = False  # not a pytest class
    name: str = Field(max_length=200)
    kind: Literal["unit", "integration", "e2e", "manual"]
    description: str = Field(max_length=1000)


class Alternative(_Strict):
    name: str = Field(max_length=120)
    pros: list[str] = Field(max_length=6)
    cons: list[str] = Field(max_length=6)


class Ambiguity(_Strict):
    question: str = Field(max_length=500)
    assumption: str = Field(max_length=500)


class Risk(_Strict):
    risk: str = Field(max_length=500)
    mitigation: str = Field(max_length=500)


class SandboxRequest(_Strict):
    optional_hosts: list[str] = Field(default=[], max_length=10)
    cpus: int = Field(default=2, ge=1, le=4)
    memory_gb: int = Field(default=4, ge=2, le=8)
    timeout_min: int = Field(default=45, ge=10, le=120)
    max_attempts: int = Field(default=3, ge=1, le=5)


class PlanDoc(_Strict):
    schema_version: Literal[1] = 1
    title: str = Field(max_length=200)
    summary: str = Field(max_length=3000)
    ambiguities: list[Ambiguity] = Field(max_length=20)
    knowledge_refs: list[int] = Field(max_length=20)
    approach: str = Field(max_length=6000)
    alternatives: list[Alternative] = Field(max_length=4)
    changes: list[FileChange] = Field(min_length=1, max_length=60)
    tests: list[TestItem] = Field(max_length=40)
    verification: list[str] = Field(min_length=1, max_length=10)
    risks: list[Risk] = Field(max_length=15)
    rollback: str = Field(max_length=2000)
    estimate: Literal["S", "M", "L", "XL"]
    commit_message: str = Field(max_length=2000)
    sandbox: SandboxRequest = SandboxRequest()


def canonical_json(content: dict[str, Any] | PlanDoc) -> str:
    data = content.model_dump(mode="json") if isinstance(content, BaseModel) else content
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(content: dict[str, Any] | PlanDoc) -> str:
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


def approval_payload_hash(content_hash_: str, acknowledged: list[str]) -> str:
    """What a plan approval binds to (03 §6): the revision's hash and exactly the acknowledged warnings."""
    payload = "\n".join([content_hash_, *sorted(set(acknowledged))])
    return hashlib.sha256(payload.encode()).hexdigest()
