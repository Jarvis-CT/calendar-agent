"""Date/time parsing and conversion.

Display is always Asia/Singapore unless configured otherwise.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ_NAME = "Asia/Singapore"
SGT = ZoneInfo(DEFAULT_TZ_NAME)
UTC = UTC

# Graph may echo Windows time zone names. Map the ones we expect to IANA names.
_WINDOWS_TO_IANA = {
    "Singapore Standard Time": "Asia/Singapore",
    "UTC": "UTC",
    "tzone://Microsoft/Utc": "UTC",
    "GMT Standard Time": "Europe/London",
    "Pacific Standard Time": "America/Los_Angeles",
    "Eastern Standard Time": "America/New_York",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
}

# Graph returns seven fractional digits; Python accepts at most six.
_FRACTION_RE = re.compile(r"(\.\d{1,6})\d*")

DATETIME_HELP = (
    "Use YYYY-MM-DD or YYYY-MM-DDTHH:MM (interpreted as Asia/Singapore), optionally with an "
    "offset such as +08:00 or Z. The words 'today' and 'tomorrow' are also accepted."
)


def zone_name(tz: tzinfo) -> str:
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return "UTC" if tz is UTC else str(tz)


def resolve_zone(name: str | None) -> tzinfo:
    """Resolve an IANA or well-known Windows time zone name."""
    if not name or name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        mapped = _WINDOWS_TO_IANA.get(name)
        if mapped == "UTC":
            return UTC
        if mapped:
            return ZoneInfo(mapped)
    raise ValueError(f"Unknown time zone received from Microsoft Graph: {name!r}")


def parse_user_datetime(text: str, *, tz: tzinfo = SGT, now: datetime | None = None) -> datetime:
    """Parse user input into an aware datetime. Naive values are taken to be in ``tz``."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError(f"Empty date/time. {DATETIME_HELP}")
    reference = now or datetime.now(tz)
    lowered = cleaned.lower()
    if lowered in {"today", "tomorrow"}:
        day = reference.date() + timedelta(days=1 if lowered == "tomorrow" else 0)
        return datetime.combine(day, time.min, tz)
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"Could not parse date/time {cleaned!r}. {DATETIME_HELP}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def start_of_day(moment: datetime, tz: tzinfo = SGT) -> datetime:
    local = moment.astimezone(tz)
    return datetime.combine(local.date(), time.min, tz)


def to_graph_datetime(moment: datetime, tz: tzinfo = SGT) -> dict[str, str]:
    """Build a Graph ``dateTimeTimeZone`` object expressed in ``tz``."""
    if moment.tzinfo is None:
        raise ValueError("Internal error: naive datetime passed to to_graph_datetime")
    local = moment.astimezone(tz)
    return {"dateTime": local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": zone_name(tz)}


def parse_graph_datetime(value: dict[str, str]) -> datetime:
    """Parse a Graph ``dateTimeTimeZone`` object into an aware datetime."""
    raw = _FRACTION_RE.sub(r"\1", value["dateTime"])
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=resolve_zone(value.get("timeZone")))


def to_google_datetime(moment: datetime, tz: tzinfo = SGT) -> dict[str, str]:
    """Build a Google Calendar ``EventDateTime`` (RFC 3339 with offset plus IANA zone)."""
    if moment.tzinfo is None:
        raise ValueError("Internal error: naive datetime passed to to_google_datetime")
    local = moment.astimezone(tz)
    return {"dateTime": local.isoformat(timespec="seconds"), "timeZone": zone_name(tz)}


def to_google_date(moment: datetime, tz: tzinfo = SGT) -> dict[str, str]:
    """Build the all-day form of a Google ``EventDateTime``: the local date only."""
    if moment.tzinfo is None:
        raise ValueError("Internal error: naive datetime passed to to_google_date")
    return {"date": moment.astimezone(tz).date().isoformat()}


def parse_google_datetime(value: dict[str, str], tz: tzinfo = SGT) -> datetime:
    """Parse a Google ``EventDateTime``: ``dateTime`` (RFC 3339) or all-day ``date``."""
    if value.get("dateTime"):
        parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=resolve_zone(value.get("timeZone")) or tz)
        return parsed
    if value.get("date"):
        day = datetime.fromisoformat(value["date"])
        zone = resolve_zone(value["timeZone"]) if value.get("timeZone") else tz
        return day.replace(tzinfo=zone)
    raise ValueError("Google event is missing both dateTime and date")


def to_rfc3339_utc(moment: datetime) -> str:
    """Format a datetime for Google ``timeMin``/``timeMax`` query parameters."""
    if moment.tzinfo is None:
        raise ValueError("Internal error: naive datetime passed to to_rfc3339_utc")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_query_utc(moment: datetime) -> str:
    """Format a datetime as the UTC ISO-8601 string Graph expects in query parameters."""
    if moment.tzinfo is None:
        raise ValueError("Internal error: naive datetime passed to to_query_utc")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_dt(moment: datetime, tz: tzinfo = SGT) -> str:
    return f"{moment.astimezone(tz):%Y-%m-%d %H:%M} ({zone_name(tz)})"


def format_range(start: datetime, end: datetime, tz: tzinfo = SGT) -> str:
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    if local_start.date() == local_end.date():
        return f"{local_start:%Y-%m-%d %H:%M}–{local_end:%H:%M} ({zone_name(tz)})"
    return f"{local_start:%Y-%m-%d %H:%M} – {local_end:%Y-%m-%d %H:%M} ({zone_name(tz)})"
