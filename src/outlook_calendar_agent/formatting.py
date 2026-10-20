"""Terminal rendering. All times are shown in the configured display zone (Asia/Singapore)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .models import (
    REMINDER_OFF,
    Attendee,
    CalendarEvent,
    CalendarInfo,
    DeleteDraft,
    EventDraft,
    TaskItem,
    UpdateDraft,
)
from .timeutil import SGT, format_dt, zone_name
from .titles import format_duration

_NONE = "(none)"


def format_signed_in(me: dict[str, Any]) -> str:
    name = me.get("displayName") or "(unknown)"
    email = me.get("mail") or me.get("userPrincipalName") or "(no e-mail)"
    return f"Signed in as: {name} <{email}>"


def format_event_line(event: CalendarEvent, tz: tzinfo = SGT) -> str:
    start, end = event.start.astimezone(tz), event.end.astimezone(tz)
    if event.is_all_day:
        when = "all day     "
    elif start.date() != end.date():
        when = f"{start:%H:%M}–{end:%m-%d %H:%M}"
    else:
        when = f"{start:%H:%M}–{end:%H:%M}"
    parts = [event.subject]
    if event.is_cancelled:
        parts[0] = f"[cancelled] {parts[0]}"
    if event.location:
        parts.append(event.location)
    if event.organizer_email:
        parts.append(event.organizer_email)
    if event.event_type and event.event_type != "singleInstance":
        parts.append("recurring")
    line = f"  {when}  {' | '.join(parts)}"
    if event.links:
        line += f"\n           🔗 {event.links[0]}"
    return line


def format_tasks(
    tasks: Sequence[TaskItem], start: datetime, end: datetime, tz: tzinfo = SGT
) -> str:
    header = (
        f"Tasks due {start.astimezone(tz):%Y-%m-%d} to {end.astimezone(tz):%Y-%m-%d}"
        " (Google Tasks; dates only)"
    )
    if not tasks:
        return f"{header}\n(no tasks)"
    lines = [header]
    current = None
    for task in tasks:
        label = task.due.isoformat() if task.due else "no due date"
        if label != current:
            lines.append(label)
            current = label
        mark = "[x]" if task.completed else "[ ]"
        extra = "" if task.in_default_list else f" | {task.list_name}"
        lines.append(f"  {mark} {task.title}{extra}")
    return "\n".join(lines)


def format_draft_line(draft_id: str, payload: object, tz: tzinfo = SGT) -> str:
    """One line per saved draft, shared by the CLI `drafts` command and the MCP server."""
    if isinstance(payload, EventDraft):
        detail = f"{payload.subject}  {format_dt(payload.start, tz)}"
    elif isinstance(payload, UpdateDraft):
        detail = f"{payload.original.subject}  {format_dt(payload.effective_start, tz)}"
    elif isinstance(payload, DeleteDraft):
        detail = f"{payload.original.subject}  {format_dt(payload.original.start, tz)}"
    else:  # pragma: no cover - the discriminated union prevents this
        detail = "(unknown draft)"
    kind = getattr(payload, "kind", "?")
    return f"{draft_id}  {kind:<6}  {detail}"


def format_calendars(calendars: Sequence[CalendarInfo]) -> str:
    if not calendars:
        return "No calendars visible."
    return "\n".join(c.display() for c in calendars)


def format_event_list(
    events: Sequence[CalendarEvent],
    start: datetime,
    end: datetime,
    tz: tzinfo = SGT,
    *,
    calendar_id: str = "primary",
    calendar_name: str | None = None,
) -> str:
    header = (
        f"Events {start.astimezone(tz):%Y-%m-%d %H:%M} to {end.astimezone(tz):%Y-%m-%d %H:%M}"
        f" — {zone_name(tz)}"
    )
    if calendar_id != "primary":
        label = f"{calendar_name} ({calendar_id})" if calendar_name else calendar_id
        header += f"\nCalendar: {label}"
    if not events:
        return f"{header}\n(no events)"
    lines = [header]
    current_day = None
    for event in events:
        day = event.start.astimezone(tz).date()
        if day != current_day:
            lines.append(f"{day:%Y-%m-%d}")
            current_day = day
        lines.append(format_event_line(event, tz))
    return "\n".join(lines)


def format_event_choices(events: Sequence[CalendarEvent], tz: tzinfo = SGT) -> str:
    lines = ["Multiple events match. Re-run with --event-id <ID> for one of:"]
    for index, event in enumerate(events, start=1):
        lines.append(f"  {index}. {format_dt(event.start, tz)}  {event.subject}")
        lines.append(f"     id: {event.id}")
    return "\n".join(lines)


def _attendees(attendees: Iterable[Attendee]) -> str:
    items = [a.display() for a in attendees]
    return "; ".join(items) if items else _NONE


def _notification_status(has_attendees: bool, acknowledged: bool) -> str:
    if not has_attendees:
        return "no (no attendees)"
    if acknowledged:
        return "YES (all attendees will be e-mailed)"
    return "no (attendees are changed on the event without an e-mail)"


def _first_link(snapshot: object) -> str:
    """The meeting link of a snapshot/event, or the placeholder."""
    links = getattr(snapshot, "links", None) or []
    return links[0] if links else _NONE


def _reminder_text(minutes: int | None) -> str:
    if minutes is None:
        return "calendar default"
    if minutes == REMINDER_OFF:
        return "none"
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return f"{days} day{'s' if days != 1 else ''} before"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''} before"
    return f"{minutes} minutes before"


def _block(title: str, rows: list[tuple[str, str]]) -> str:
    width = max(len(label) for label, _ in rows)
    lines = [title, "-" * len(title)]
    lines += [f"{label.ljust(width)} : {value}" for label, value in rows]
    return "\n".join(lines)


def format_conflicts(conflicts: Sequence[CalendarEvent], tz: tzinfo = SGT) -> str:
    if not conflicts:
        return "Conflicts: none in the proposed window."
    lines = [f"Conflicts: {len(conflicts)} existing event(s) overlap the proposed time:"]
    lines += [f"{format_event_line(e, tz)}  [calendar: {e.calendar_id}]" for e in conflicts]
    return "\n".join(lines)


def format_days(start: datetime, end: datetime, tz: tzinfo = SGT) -> str:
    """'1 day' or '13 days' for an all-day span (end is the midnight after the last day)."""
    days = max(1, (end.astimezone(tz).date() - start.astimezone(tz).date()).days)
    return f"{days} day" + ("" if days == 1 else "s")


def _all_day_label(moment: datetime, tz: tzinfo) -> str:
    return f"{moment.astimezone(tz):%Y-%m-%d} (all day)"


def format_create_preview(draft: EventDraft, tz: tzinfo = SGT) -> str:
    if draft.all_day:
        when = [
            ("Start", _all_day_label(draft.start, tz)),
            ("End", _all_day_label(draft.end - timedelta(days=1), tz)),
            ("Duration", format_days(draft.start, draft.end, tz) + ", all day"),
        ]
    else:
        when = [
            ("Start", format_dt(draft.start, tz)),
            ("End", format_dt(draft.end, tz)),
            (
                "Duration",
                format_duration(int((draft.end - draft.start).total_seconds() // 60))
                + (
                    ' (default; reply e.g. "make it 2 hours" to change)'
                    if draft.duration_defaulted
                    else ""
                ),
            ),
        ]
    rows = [
        ("Action", "CREATE event"),
        ("Event ID", "(assigned by the calendar provider on creation)"),
        ("Subject", draft.subject),
        *when,
        ("Calendar", draft.calendar_label),
        ("Location", draft.location or _NONE),
        ("Meeting link", draft.link or "(none — Meet/Teams links are not created by this tool)"),
        ("Reminder", _reminder_text(draft.reminder_minutes_before)),
        ("Attendees", _attendees(draft.attendees)),
        ("Invitations sent", _notification_status(bool(draft.attendees), draft.send_invitations)),
        ("Recurrence", "none (single event)"),
    ]
    if draft.body:
        rows.append(("Notes", draft.body))
    return _block("PROPOSED CHANGE", rows)


def format_update_preview(draft: UpdateDraft, tz: tzinfo = SGT) -> str:
    original = draft.original

    def change(label: str, old: str, new: str | None) -> tuple[str, str]:
        if new is None or new == old:
            return (label, old)
        return (label, f"{old}  ->  {new}   [CHANGED]")

    changed = set(draft.changed_fields())
    if original.is_all_day:

        def stamp(moment: datetime, *, is_end: bool = False) -> str:
            return _all_day_label(moment - timedelta(days=int(is_end)), tz)

    else:

        def stamp(moment: datetime, *, is_end: bool = False) -> str:
            return format_dt(moment, tz)

    rows = [
        (
            "Action",
            f"UPDATE event (PATCH /me/events/{{id}}) — fields: {', '.join(sorted(changed))}",
        ),
        ("Event ID", original.id),
        change("Subject", original.subject, draft.subject if "subject" in changed else None),
        change(
            "Start",
            stamp(original.start),
            stamp(draft.effective_start) if "start" in changed else None,
        ),
        change(
            "End",
            stamp(original.end, is_end=True),
            stamp(draft.effective_end, is_end=True) if "end" in changed else None,
        ),
        ("Calendar", original.calendar_label),
        change(
            "Location",
            original.location or _NONE,
            draft.location if "location" in changed else None,
        ),
        change(
            "Meeting link",
            _first_link(original),
            (draft.link or _NONE) if "link" in changed else None,
        ),
        change(
            "Reminder",
            _reminder_text(original.reminder_minutes_before),
            _reminder_text(draft.effective_reminder) if "reminder" in changed else None,
        ),
        ("Attendees", _attendees(original.attendees)),
        (
            "Update notices sent",
            _notification_status(bool(original.attendees), draft.notify_attendees),
        ),
        ("Recurrence", "none (single event)"),
    ]
    return _block("PROPOSED CHANGE", rows)


def format_delete_preview(draft: DeleteDraft, tz: tzinfo = SGT) -> str:
    original = draft.original
    rows = [
        ("Action", "DELETE event — PERMANENT"),
        ("Event ID", original.id),
        ("Subject", original.subject),
        ("Start", format_dt(original.start, tz)),
        ("End", format_dt(original.end, tz)),
        ("Calendar", original.calendar_label),
        ("Location", original.location or _NONE),
        ("Meeting link", _first_link(original)),
        ("Attendees", _attendees(original.attendees)),
        (
            "Cancellations sent",
            _notification_status(bool(original.attendees), draft.notify_attendees),
        ),
        (
            "Your role",
            "organizer"
            if original.is_organizer
            else "attendee (removes it from your calendar only)",
        ),
    ]
    return _block("PROPOSED DELETION", rows)
