"""Rule R9: every enum the API exposes is a Literal whose values match the StrEnum (and so the DB CHECK)."""

from app import enums
from app.main import app

# (schema, property) -> the StrEnum it must match.
EXPECTED = {
    ("UserOut", "role"): enums.Role,
    ("UserCreate", "role"): enums.Role,
    ("app__chat__routes__SessionOut", "source"): enums.SessionSource,
    ("app__chat__routes__SessionOut", "visibility"): enums.Visibility,
    ("SessionUpdate", "visibility"): enums.Visibility,
    ("MessageOut", "status"): enums.MessageStatus,
    ("DocumentOut", "source_type"): enums.SourceType,
    ("DocumentOut", "visibility"): enums.Visibility,
    ("HitOut", "source_type"): enums.SourceType,
    ("HitOut", "visibility"): enums.Visibility,
    ("PageSummary", "visibility"): enums.Visibility,
    ("VisibilityIn", "visibility"): enums.Visibility,
    ("ProjectOut", "host"): enums.GitHost,
    ("ProjectOut", "visibility"): enums.Visibility,
    ("ProjectOut", "status"): enums.ProjectStatus,
    ("ProjectCreate", "host"): enums.GitHost,
    ("LibrarianRunOut", "status"): enums.LibrarianRunStatus,
}


def _enum_values(prop: dict) -> set[str]:
    if "enum" in prop:
        return set(prop["enum"])
    for alt in prop.get("anyOf", []):
        if "enum" in alt:
            return set(alt["enum"])
    return set()


def test_api_enums_match_the_str_enums():
    schemas = app.openapi()["components"]["schemas"]
    for (schema, prop), enum in EXPECTED.items():
        values = _enum_values(schemas[schema]["properties"][prop])
        assert values == {e.value for e in enum}, f"{schema}.{prop}"


def test_no_enum_like_field_is_a_plain_string():
    """New models get the same treatment: these names are always enums."""
    names = {"visibility", "source_type", "host"}
    schemas = app.openapi()["components"]["schemas"]
    loose = [
        f"{name}.{prop}"
        for name, schema in schemas.items()
        for prop, spec in schema.get("properties", {}).items()
        if prop in names and not _enum_values(spec)
    ]
    assert loose == []
