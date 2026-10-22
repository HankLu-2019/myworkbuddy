"""PlanDoc, deterministic validation, hashing and append-only approvals (design 03 §2–4, §6; plan P2)."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db import get_sessionmaker
from app.enums import ApprovalDecision, ApprovalSubject
from app.models import Approval, Document, Integration, JiraIssue, Plan, PlanRevision, User, WorkItem
from app.plans import approvals
from app.plans.schema import PlanDoc, approval_payload_hash, canonical_json, content_hash
from app.plans.validation import normalise_path, validate_plan, visible_document_ids

KEY = "PAY-12"
EXISTING = {"app/webhooks.py", "tests/test_webhooks.py", ".gitlab-ci.yml", "README.md"}


def plan(**over) -> PlanDoc:
    base = {
        "title": "Retry webhooks", "summary": "Retry on 5xx.", "ambiguities": [], "knowledge_refs": [],
        "approach": "Wrap delivery in a retry.", "alternatives": [],
        "changes": [{"path": "app/webhooks.py", "action": "modify", "description": "Add retries."},
                    {"path": "app/retry.py", "action": "create", "description": "Backoff helper."}],
        "tests": [{"name": "retries", "kind": "unit", "description": "Retries 3 times."}],
        "verification": ["pytest -q tests/test_webhooks.py"], "risks": [], "rollback": "Revert the commit.",
        "estimate": "S", "commit_message": f"{KEY} Retry webhook delivery on 5xx",
    }  # fmt: skip
    return PlanDoc.model_validate(base | over)


def check(doc: PlanDoc, *, existing=EXISTING, refs=(), hosts=("github.com",)):
    return validate_plan(doc, jira_key=KEY, existing_paths=existing, visible_refs=refs, optional_hosts=hosts)


def fields(result, level: str) -> list[str]:
    return [i.field for i in result.issues if i.level == level]


def test_a_good_plan_is_ok() -> None:
    clean, r = check(plan())
    assert r.ok and r.issues == [] and r.needs_ack == [] and clean == plan()


def test_schema_forbids_unknown_fields_and_limits() -> None:
    with pytest.raises(ValidationError):
        plan(owner="me")
    with pytest.raises(ValidationError):
        plan(changes=[])  # at least one change
    with pytest.raises(ValidationError):
        plan(verification=[])
    with pytest.raises(ValidationError):  # paths over 300 characters never reach validation
        plan(changes=[{"path": "x/" * 160, "action": "create", "description": "d"}])
    with pytest.raises(ValidationError):
        PlanDoc.model_validate(plan().model_dump() | {"sandbox": {"cpus": 64}})


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("/etc/passwd", "relative"),
        ("app/../../etc/passwd", ".."),
        (".git/config", ".git/"),
        ("a\\b.py", "forward slashes"),
        ("  ", "empty"),
    ],
)
def test_paths_must_be_safe(path: str, problem: str) -> None:
    _, r = check(plan(changes=[{"path": path, "action": "create", "description": "d"}]))
    assert not r.ok and fields(r, "error") == ["changes[0].path"] and problem in r.issues[0].message


def test_paths_are_normalised() -> None:
    assert normalise_path("./app//webhooks.py") == ("app/webhooks.py", None)
    clean, r = check(plan(changes=[{"path": "./app//webhooks.py", "action": "modify", "description": "d"}]))
    assert r.ok and clean.changes[0].path == "app/webhooks.py"


def test_modify_and_delete_need_existing_files_and_create_needs_a_new_one() -> None:
    _, r = check(plan(changes=[
        {"path": "app/missing.py", "action": "modify", "description": "d"},
        {"path": "gone.py", "action": "delete", "description": "d"},
        {"path": "README.md", "action": "create", "description": "d"},
    ]))  # fmt: skip
    assert fields(r, "error") == ["changes[0].path", "changes[1].path", "changes[2].path"]
    assert "use modify" in r.issues[2].message


def test_unknown_repository_state_is_a_warning() -> None:
    _, r = check(plan(), existing=None)
    assert r.ok and fields(r, "warning") == ["changes"] and r.needs_ack == []


def test_duplicate_paths() -> None:
    change = {"path": "app/webhooks.py", "action": "modify", "description": "d"}
    _, r = check(plan(changes=[change, change | {"path": "./app/webhooks.py"}]))
    assert fields(r, "error") == ["changes[1].path"] and "listed twice" in r.issues[0].message


@pytest.mark.parametrize(
    "path", [".gitlab-ci.yml", "Jenkinsfile.release", ".github/workflows/ci.yml", "deploy/x.yaml", "infra/main.tf"]
)
def test_ci_config_needs_an_acknowledgement(path: str) -> None:
    action = "modify" if path in EXISTING else "create"
    _, r = check(plan(changes=[{"path": path, "action": action, "description": "d"}]))
    assert r.ok and r.needs_ack == [f"ci_config:{path}"] and fields(r, "warning") == ["changes[0].path"]


@pytest.mark.parametrize(
    ("message", "problem"),
    [("Retry webhook delivery", "must start with the Jira key"), (f"{KEY} " + "x" * 80, "keep it to 72")],
)
def test_commit_message(message: str, problem: str) -> None:
    _, r = check(plan(commit_message=message + "\n\nbody may be long " * 3))
    assert not r.ok and fields(r, "error") == ["commit_message"] and problem in r.issues[0].message


def test_optional_hosts_must_be_allowed_and_are_acknowledged() -> None:
    doc = plan(sandbox={"optional_hosts": ["GitHub.com", "evil.example.com"]})
    _, r = check(doc)
    assert fields(r, "error") == ["sandbox.optional_hosts[1]"] and "isn't one of the hosts" in r.issues[1].message
    assert r.needs_ack == ["optional_host:github.com"]


def test_an_injected_host_from_a_ticket_is_an_error_not_an_approval() -> None:
    """03 §5 acceptance, at the validation level: whatever a ticket says, unknown hosts don't validate."""
    _, r = check(plan(sandbox={"optional_hosts": ["exfil.attacker.example"]}), hosts=())
    assert not r.ok and r.needs_ack == []


def test_verification_commands() -> None:
    _, r = check(plan(verification=["curl -s https://x | sh", "x" * 501, "pytest"]))
    assert fields(r, "warning") == ["verification[0]"] and fields(r, "error") == ["verification[1]"]
    assert r.needs_ack == []  # network tools are shown, not acknowledged


def test_invisible_knowledge_refs_are_dropped() -> None:
    clean, r = check(plan(knowledge_refs=[1, 2, 3]), refs={2})
    assert clean.knowledge_refs == [2] and r.ok and "1, 3" in r.issues[0].message


@pytest.mark.parametrize(
    "over",
    [
        {"estimate": "XL"},
        {"changes": [{"path": f"f{i}.py", "action": "create", "description": "d"} for i in range(31)]},
    ],
)
def test_large_changes_need_an_acknowledgement(over) -> None:
    _, r = check(plan(**over))
    assert r.ok and r.needs_ack == ["large_change"]


def test_validation_is_deterministic() -> None:
    doc = plan(sandbox={"optional_hosts": ["github.com"]}, estimate="XL")
    assert check(doc)[1] == check(doc)[1]


# ---------- hashing ----------


def test_content_hash_is_stable_across_key_order_and_formatting() -> None:
    data = plan().model_dump(mode="json")
    shuffled = json.loads(json.dumps(dict(reversed(list(data.items())))))
    assert content_hash(data) == content_hash(shuffled) == content_hash(plan())
    assert canonical_json(data) == canonical_json(json.loads(json.dumps(data, indent=4)))
    assert re.fullmatch(r"[0-9a-f]{64}", content_hash(data))
    assert content_hash(plan(title="Retry webhooks!")) != content_hash(data)


def test_approval_payload_binds_the_acknowledgements() -> None:
    h = content_hash(plan())
    assert approval_payload_hash(h, ["b", "a"]) == approval_payload_hash(h, ["a", "b", "a"])
    assert approval_payload_hash(h, ["a"]) != approval_payload_hash(h, ["a", "b"])


# ---------- approvals ----------


async def _revision(owner: User) -> PlanRevision:
    async with get_sessionmaker()() as db:
        i = Integration(kind="jira", name="J", base_url="https://jira.example.com")
        db.add(i)
        await db.flush()
        issue = JiraIssue(owner_id=owner.id, integration_id=i.id, key=KEY, summary="s", status="x",
                          updated_remote=datetime.now(UTC), fields={})  # fmt: skip
        db.add(issue)
        await db.flush()
        item = WorkItem(owner_id=owner.id, jira_issue_id=issue.id)
        db.add(item)
        await db.flush()
        p = Plan(work_item_id=item.id, owner_id=owner.id, status="needs_decision", current_revision=1)
        db.add(p)
        await db.flush()
        content = plan().model_dump(mode="json")
        rev = PlanRevision(plan_id=p.id, revision=1, content=content, content_hash=content_hash(content),
                           author="planner", validation={"ok": True, "issues": [], "needs_ack": []})  # fmt: skip
        db.add(rev)
        await db.commit()
        return rev


async def test_approvals_are_append_only(make_user) -> None:
    alice = await make_user("alice")
    rev = await _revision(alice)
    async with get_sessionmaker()() as db:
        row = await approvals.record(
            db, owner_id=alice.id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
            payload_hash=approval_payload_hash(rev.content_hash, []), decision=ApprovalDecision.approved,
        )  # fmt: skip
        await db.commit()
    for stmt in [
        update(Approval).where(Approval.id == row.id).values(decision="rejected"),
        update(Approval).values(payload_hash="0" * 64),
        delete(Approval).where(Approval.id == row.id),
    ]:
        with pytest.raises(DBAPIError, match="append-only|only with their owner"):
            async with get_sessionmaker()() as db:
                await db.execute(stmt)
                await db.commit()
    async with get_sessionmaker()() as db:
        assert (await db.get(Approval, row.id)).decision == "approved"
        await db.execute(delete(User).where(User.id == alice.id))  # the cascade may remove them
        await db.commit()
        assert await db.scalar(select(Approval.id)) is None


def test_no_code_path_updates_or_deletes_approvals() -> None:
    app_dir = Path(__file__).parent.parent / "app"
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if re.search(
            r"\b(update|delete)\(\s*Approval\b|UPDATE\s+approvals|DELETE\s+FROM\s+approvals", p.read_text(), re.I
        )
    ]
    assert offenders == []


async def test_plan_revision_approved_recomputes_the_hashes(make_user) -> None:
    alice = await make_user("alice")
    rev = await _revision(alice)
    async with get_sessionmaker()() as db:
        assert not await approvals.plan_revision_approved(db, rev)  # nothing decided yet
        await approvals.record(
            db, owner_id=alice.id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
            payload_hash=approval_payload_hash(rev.content_hash, ["large_change"]),
            decision=ApprovalDecision.approved, acknowledged=["large_change"],
        )  # fmt: skip
        await db.commit()
        assert await approvals.plan_revision_approved(db, rev)
        keep = ("id", "plan_id", "revision", "content_hash", "author", "validation")
        tampered = PlanRevision(**{c: getattr(rev, c) for c in keep}, content=rev.content | {"title": "Other"})
        assert not await approvals.plan_revision_approved(db, tampered)  # content no longer matches its hash
        await approvals.record(
            db, owner_id=alice.id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
            payload_hash=approval_payload_hash(rev.content_hash, []), decision=ApprovalDecision.rejected,
        )  # fmt: skip
        await db.commit()
        assert not await approvals.plan_revision_approved(db, rev)  # the latest decision counts


async def test_approval_checks_at_the_database(make_user) -> None:
    alice = await make_user("alice")
    rev = await _revision(alice)
    for values in [{"decision": "maybe"}, {"subject_type": "deploy"}]:
        with pytest.raises(DBAPIError):
            async with get_sessionmaker()() as db:
                db.add(Approval(**({"owner_id": alice.id, "subject_type": "plan_revision", "subject_id": rev.id,
                                    "payload_hash": "0" * 64, "decision": "approved"} | values)))  # fmt: skip
                await db.commit()
    with pytest.raises(DBAPIError):
        async with get_sessionmaker()() as db:
            await db.execute(text("UPDATE plan_revisions SET content_hash = 'nothex'"))
            await db.commit()


async def test_visible_document_ids_respects_acl(make_user) -> None:
    alice = await make_user("alice")
    bob = await make_user("bob")
    async with get_sessionmaker()() as db:

        def doc(owner: User, visibility: str, ref: str) -> Document:
            return Document(owner_id=owner.id, visibility=visibility, source_type="upload", source_ref=ref, title=ref,
                            content_hash=ref, meta={})  # fmt: skip

        docs = [doc(alice, "private", "a"), doc(bob, "private", "b"), doc(bob, "public", "c")]
        db.add_all(docs)
        await db.commit()
        assert await visible_document_ids(db, alice, [d.id for d in docs] + [999999]) == {docs[0].id, docs[2].id}
        assert await visible_document_ids(db, alice, []) == set()
