"""Cron rules for schedules (design 02 §4 "Cron rules").

Standard 5-field expressions, evaluated in the schedule's own timezone:

- a local time that doesn't exist (spring forward) is skipped;
- a local time that happens twice (fall back) fires once, at its first occurrence.

croniter walks naive local wall-clock times; zoneinfo turns each into an instant. That keeps the DST rules in
one place instead of relying on croniter's own timezone handling.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from cron_descriptor import Options, get_description
from croniter import CroniterBadDateError, croniter

MAX_STEPS = 5000  # candidates examined per lookup; a valid 5-field expression fires within ~4 years
HORIZON = 200  # occurrences checked for the minimum interval


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Preview:
    valid: bool
    error: str | None
    description: str | None
    next: list[str]  # ISO times in the schedule's timezone


def zone(name: str) -> ZoneInfo:
    if name not in _zones():
        raise CronError(f"Unknown timezone {name!r}; use an IANA name such as Europe/Stockholm.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - listed but unloadable
        raise CronError(f"Unknown timezone {name!r}") from None


_ZONES: set[str] | None = None


def _zones() -> set[str]:
    global _ZONES
    if _ZONES is None:
        _ZONES = available_timezones() | {"UTC"}
    return _ZONES


def validate(expr: str) -> str:
    """The normalised expression (single spaces), or CronError with a message for people."""
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("Use 5 fields: minute hour day-of-month month day-of-week, e.g. 0 8 * * 1-5.")
    normalised = " ".join(fields)
    if not croniter.is_valid(normalised):
        raise CronError(f"{normalised!r} is not a valid cron expression.")
    return normalised


def _instant(local: datetime, tz: ZoneInfo) -> datetime | None:
    """The UTC instant of a naive local time: None in a DST gap, the first occurrence in an overlap."""
    aware = local.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) != local:
        return None  # doesn't exist on that day
    return utc


def occurrences(expr: str, tz: ZoneInfo, after: datetime, count: int) -> list[datetime]:
    """The next `count` fire times strictly after `after` (aware), as UTC datetimes."""
    after = after.astimezone(UTC)
    # Start an hour early in local time: inside a fall-back overlap the naive time of `after` is ambiguous.
    start = (after.astimezone(tz) - timedelta(hours=1)).replace(tzinfo=None)
    it = croniter(expr, start)
    found: list[datetime] = []
    for _ in range(MAX_STEPS):
        if len(found) == count:
            break
        try:
            local = it.get_next(datetime)
        except CroniterBadDateError:  # e.g. 31 February: never fires
            break
        utc = _instant(local, tz)
        if utc is None or utc <= after or (found and utc <= found[-1]):
            continue
        found.append(utc)
    return found


def next_after(expr: str, tz_name: str, after: datetime) -> datetime:
    found = occurrences(expr, zone(tz_name), after, 1)
    if not found:
        raise CronError("This expression never fires.")
    return found[0]


def min_gap_minutes(expr: str, tz_name: str, now: datetime) -> float:
    times = occurrences(expr, zone(tz_name), now, HORIZON)
    if len(times) < 2:
        return float("inf")
    return min((b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)) / 60


def describe(expr: str) -> str:
    options = Options()
    options.use_24hour_time_format = True
    options.verbose = False
    return get_description(expr, options)


def check(expr: str, tz_name: str, *, min_interval_min: int, now: datetime) -> str:
    """Validate the expression, timezone and minimum interval. Returns the normalised expression."""
    normalised = validate(expr)
    zone(tz_name)
    gap = min_gap_minutes(normalised, tz_name, now)
    if gap == float("inf") and not occurrences(normalised, zone(tz_name), now, 1):
        raise CronError("This expression never fires.")
    if gap < min_interval_min:
        raise CronError(f"Runs would be {gap:g} minutes apart; the minimum is {min_interval_min} minutes.")
    return normalised


def preview(expr: str, tz_name: str, *, min_interval_min: int, now: datetime, count: int = 5) -> Preview:
    try:
        normalised = check(expr, tz_name, min_interval_min=min_interval_min, now=now)
    except CronError as exc:
        return Preview(valid=False, error=str(exc), description=None, next=[])
    tz = zone(tz_name)
    return Preview(
        valid=True,
        error=None,
        description=describe(normalised),
        next=[t.astimezone(tz).isoformat() for t in occurrences(normalised, tz, now, count)],
    )
