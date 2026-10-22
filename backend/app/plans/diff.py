"""Field-level diff between two plan revisions (design 03 §8), computed on the server so every client shows the
same thing. Lists of objects are matched by their natural key (a change by path, a test by name, ...), so moving
an item or editing one field of it shows as one `changed` entry, not a removal and an addition.
"""

from typing import Any, Literal

from pydantic import BaseModel

KEYS = {"changes": "path", "tests": "name", "alternatives": "name", "ambiguities": "question", "risks": "risk"}


class FieldDiff(BaseModel):
    field: str  # "approach", "sandbox.cpus", "changes"
    key: str | None = None  # the item's key in a list field: "app/webhooks.py"
    kind: Literal["added", "removed", "changed", "reordered"]
    before: Any = None
    after: Any = None


def _items(field: str, before: list[Any], after: list[Any]) -> list[FieldDiff]:
    key = KEYS.get(field)
    if key is None:  # a list of plain values: what was added or removed
        out = [FieldDiff(field=field, key=str(v), kind="removed", before=v) for v in before if v not in after]
        out += [FieldDiff(field=field, key=str(v), kind="added", after=v) for v in after if v not in before]
        common = [v for v in before if v in after]
        if not out and common != [v for v in after if v in before]:
            out.append(FieldDiff(field=field, kind="reordered", before=before, after=after))
        return out
    old = {str(i.get(key)): i for i in before}
    new = {str(i.get(key)): i for i in after}
    out = []
    for k, item in old.items():
        if k not in new:
            out.append(FieldDiff(field=field, key=k, kind="removed", before=item))
        elif new[k] != item:
            out.append(FieldDiff(field=field, key=k, kind="changed", before=item, after=new[k]))
    out += [FieldDiff(field=field, key=k, kind="added", after=item) for k, item in new.items() if k not in old]
    common_old = [k for k in old if k in new]
    if common_old != [k for k in new if k in old]:
        out.append(FieldDiff(field=field, kind="reordered", before=common_old, after=[k for k in new if k in old]))
    return out


def diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> list[FieldDiff]:
    out: list[FieldDiff] = []
    for name in list(before) + [k for k in after if k not in before]:
        field = f"{prefix}{name}"
        a, b = before.get(name), after.get(name)
        if a == b:
            continue
        if name not in before:
            out.append(FieldDiff(field=field, kind="added", after=b))
        elif name not in after:
            out.append(FieldDiff(field=field, kind="removed", before=a))
        elif isinstance(a, dict) and isinstance(b, dict):
            out += diff(a, b, prefix=f"{field}.")
        elif isinstance(a, list) and isinstance(b, list):
            out += _items(field, a, b)
        else:
            out.append(FieldDiff(field=field, kind="changed", before=a, after=b))
    return out
