"""System agents from app/agents/seed/*.md (design 02 §3).

Each file is YAML frontmatter (slug, name, description, model_alias, tools, max_tier, report, inputs_schema)
followed by the system prompt. `sync_system_agents()` runs from `mwb sync-agents` in the one-shot `migrate`
service after Alembic: it inserts new agents, updates changed ones (only when something differs, so
`updated_at` means "the definition changed") and disables system agents whose file was removed. Rows are never
deleted, because schedules and runs reference them.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AgentModel, Tier
from app.llm.untrusted import with_notice
from app.models import Agent
from app.tools.registry import REGISTRY, TIER_ORDER

SEED_DIR = Path(__file__).parent / "seed"
FIELDS = ("name", "description", "model_alias", "system_prompt", "tools", "max_tier", "inputs_schema", "report")


class SeedError(ValueError):
    pass


@dataclass(frozen=True)
class Seed:
    slug: str
    name: str
    description: str
    model_alias: str
    system_prompt: str
    tools: list[str]
    max_tier: str = "read"
    inputs_schema: dict[str, Any] = field(default_factory=dict)
    report: bool = True

    def values(self) -> dict[str, Any]:
        return {"slug": self.slug, **{f: getattr(self, f) for f in FIELDS}}


def parse_seed(path: Path) -> Seed:
    text = path.read_text()
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise SeedError(f"{path.name}: needs YAML frontmatter between --- lines")
    head, body = text[4:].split("\n---\n", 1)
    meta = yaml.safe_load(head) or {}
    unknown = set(meta) - ({"slug", *FIELDS} - {"system_prompt"})  # the prompt is the body
    if unknown:
        raise SeedError(f"{path.name}: unknown fields {sorted(unknown)}")
    if meta.get("slug") != path.stem:
        raise SeedError(f"{path.name}: slug must be {path.stem!r}")
    try:
        seed = Seed(**meta, system_prompt=with_notice(body.strip()))  # every system prompt ends with the notice
    except TypeError as exc:
        raise SeedError(f"{path.name}: {exc}") from None
    _validate(path.name, seed)
    return seed


def _validate(name: str, seed: Seed) -> None:
    if seed.model_alias not in AgentModel.__members__:
        raise SeedError(f"{name}: model_alias must be one of {list(AgentModel)}")
    if seed.max_tier not in Tier.__members__:
        raise SeedError(f"{name}: max_tier must be one of {list(Tier)}")
    if not seed.system_prompt.strip() or not seed.tools:
        raise SeedError(f"{name}: needs a prompt and at least one tool")
    for tool in seed.tools:
        spec = REGISTRY.get(tool)
        if spec is None:
            raise SeedError(f"{name}: unknown tool {tool!r}")
        if TIER_ORDER[spec.tier] > TIER_ORDER[Tier(seed.max_tier)]:
            raise SeedError(f"{name}: tool {tool!r} is above max_tier {seed.max_tier}")
    try:
        Draft202012Validator.check_schema(seed.inputs_schema)
    except SchemaError as exc:
        raise SeedError(f"{name}: inputs_schema is not a valid JSON Schema: {exc.message}") from None
    if seed.inputs_schema and seed.inputs_schema.get("type") != "object":
        raise SeedError(f"{name}: inputs_schema must describe an object")


def load_seeds(directory: Path = SEED_DIR) -> list[Seed]:
    return [parse_seed(p) for p in sorted(directory.glob("*.md"))]


async def sync_system_agents(db: AsyncSession, seeds: list[Seed] | None = None) -> dict[str, int]:
    """Idempotent: a second run with the same files changes nothing. The caller commits."""
    seeds = load_seeds() if seeds is None else seeds
    counts = {"created": 0, "updated": 0, "disabled": 0, "unchanged": 0}
    for seed in seeds:
        existing = await db.scalar(select(Agent).where(Agent.slug == seed.slug))
        if existing is not None and existing.owner_id is not None:
            raise SeedError(f"{seed.slug}: a user agent already has this slug")
        stmt = insert(Agent).values(**seed.values(), enabled=True)
        changed = [getattr(Agent, f).is_distinct_from(getattr(stmt.excluded, f)) for f in FIELDS]
        stmt = stmt.on_conflict_do_update(
            index_elements=[Agent.slug],
            set_={**{f: getattr(stmt.excluded, f) for f in FIELDS}, "enabled": True, "updated_at": func.now()},
            where=Agent.owner_id.is_(None) & (Agent.enabled.is_(False) | or_(*changed)),
        ).returning(Agent.id)
        wrote = (await db.execute(stmt)).scalar()
        key = "unchanged" if wrote is None else ("created" if existing is None else "updated")
        counts[key] += 1
    result = await db.execute(
        update(Agent)
        .where(Agent.owner_id.is_(None), Agent.enabled, Agent.slug.not_in([s.slug for s in seeds] or [""]))
        .values(enabled=False, updated_at=func.now())
    )
    counts["disabled"] = result.rowcount or 0  # type: ignore[attr-defined]
    await db.flush()
    db.expire_all()
    return counts
