"""Programmatic API used by the MCP server (and usable by any other front end).

Every function takes the Runtime and plain arguments, returns human-readable text, and never
raises for expected conditions: ambiguities come back as ``QUESTION: ...``, errors as
``ERROR: ...``. Mutations still go through ``write_flow.confirm_and_execute`` and only happen in
``confirm_draft`` when the caller passes the user's literal reply, the single word ``yes``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from functools import partial

from .calendars import find_conflicts_everywhere, resolve_calendar
from .confirm import is_exact_confirmation
from .dates import (
    DateAmbiguity,
    resolve_days,
    resolve_moment,
    resolve_time,
    resolve_window,
    strip_all_day,
)
from .drafts import draft_ids
from .errors import AgentError, UserCancelledError
from .formatting import (
    format_calendars,
    format_conflicts,
    format_create_preview,
    format_delete_preview,
    format_draft_line,
    format_event_list,
    format_tasks,
    format_update_preview,
)
from .models import (
    DEFAULT_REMINDER_MINUTES,
    REMINDER_OFF,
    DeleteDraft,
    EventDraft,
    UpdateDraft,
)
from .runtime import Runtime
from .timeutil import format_dt
from .titles import clean_subject_and_location
from .write_flow import (
    build_delete_draft,
    build_update_draft,
    check_notification_policy,
    confirm_and_execute,
    ensure_write_allowed,
    first_pydantic_message,
    parse_attendees,
    resolve_event,
    summary_for_create,
    summary_for_delete,
    summary_for_update,
    verify_unchanged,
)

DEFAULT_EVENT_MINUTES = 60  # used, and labelled in the preview, when the user gives no length
MAX_EVENT_MINUTES = 7 * 24 * 60
PAST_START_GRACE = timedelta(minutes=15)  # "now" and "a few minutes ago" are fine
QUESTION = "QUESTION: "
ERROR = "ERROR: "


def _reminder_arg(value: int | None) -> int | None:
    """Tool argument -> model value: >0 minutes, 0 = no reminder, <0 = calendar default."""
    if value is None or value < 0:
        return None
    return REMINDER_OFF if value == 0 else value


def _event_window(rt: Runtime, on: str, now: datetime) -> tuple[datetime, datetime]:
    window = resolve_window(on, now=now, tz=rt.tz)
    return window.start, window.end


_REJECTIONS = {"", "no", "cancel", "stop", "nope", "nah", "expired", "discard", "不要", "取消"}


def _is_rejection(reply: str) -> bool:
    """Replies that mean 'drop this draft'. Anything else keeps it (e.g. a clarifying question)."""
    return reply.strip().lower().rstrip(".!") in _REJECTIONS


def _guard(func: Callable[[], str]) -> str:
    try:
        return func()
    except DateAmbiguity as exc:
        return QUESTION + exc.question
    except AgentError as exc:
        return ERROR + exc.message + (f" ({exc.hint})" if exc.hint else "")


def _now(rt: Runtime) -> datetime:
    return datetime.now(rt.tz)


# ---- reads ----------------------------------------------------------------------------------
def now_text(rt: Runtime) -> str:
    now = _now(rt)
    return f"Now: {now:%A %d %B %Y, %H:%M} ({rt.settings.timezone_name})"


def list_calendars_text(rt: Runtime) -> str:
    return _guard(lambda: format_calendars(rt.calendar.list_calendars_cached()))


def list_events_text(rt: Runtime, when: str = "", calendar: str = "") -> str:
    def run() -> str:
        window = resolve_window(when, now=_now(rt), tz=rt.tz)
        target = resolve_calendar(rt.calendar, calendar)
        events = rt.calendar.list_events(window.start, window.end, calendar_id=target.id)
        header = f"Events for {window.label}:"
        body = format_event_list(
            events,
            window.start,
            window.end,
            rt.tz,
            calendar_id=target.id,
            calendar_name=target.name,
        )
        return f"{header}\n{body}"

    return _guard(run)


def find_free_slots_text(
    rt: Runtime,
    day: str,
    duration_minutes: int = 30,
    earliest: str = "09:00",
    latest: str = "18:00",
    calendar: str = "",
) -> str:
    def run() -> str:
        window = resolve_window(day, now=_now(rt), tz=rt.tz)
        start_t = resolve_time(earliest) or time(9, 0)
        end_t = resolve_time(latest) or time(18, 0)
        target = resolve_calendar(rt.calendar, calendar)
        busy = [
            (e.start, e.end)
            for e in find_conflicts_everywhere(rt.calendar, window.start, window.end, target=target)
            if not e.is_all_day
        ]
        slots: list[tuple[datetime, datetime]] = []
        now = _now(rt)
        day_cursor = window.start
        while day_cursor < window.end:
            cursor = datetime.combine(day_cursor.date(), start_t, rt.tz)
            if cursor < now:  # never offer a slot that has already passed
                rounded = now.replace(second=0, microsecond=0)
                rounded += timedelta(minutes=(15 - rounded.minute % 15) % 15)
                cursor = max(cursor, rounded)
            day_end = datetime.combine(day_cursor.date(), end_t, rt.tz)
            for b_start, b_end in sorted(busy):
                if b_end <= cursor or b_start >= day_end:
                    continue
                if b_start - cursor >= timedelta(minutes=duration_minutes):
                    slots.append((cursor, b_start))
                cursor = max(cursor, b_end)
            if day_end - cursor >= timedelta(minutes=duration_minutes):
                slots.append((cursor, day_end))
            day_cursor += timedelta(days=1)
        if not slots:
            return (
                f"No free slot of {duration_minutes} minutes between {start_t:%H:%M} and "
                f"{end_t:%H:%M} for {window.label}."
            )
        lines = [f"Free slots of at least {duration_minutes} minutes for {window.label}:"]
        lines += [f"  {s:%a %d %b %H:%M}–{e:%H:%M}" for s, e in slots[:12]]
        return "\n".join(lines)

    return _guard(run)


def list_tasks_text(rt: Runtime, when: str = "", include_completed: bool = False) -> str:
    def run() -> str:
        if rt.tasks is None:
            raise AgentError("Google Tasks is not enabled in this configuration.")
        window = resolve_window(when, now=_now(rt), tz=rt.tz)
        tasks = rt.tasks.list_all_tasks(
            window.start, window.end, include_completed=include_completed
        )
        return format_tasks(tasks, window.start, window.end, rt.tz)

    return _guard(run)


# ---- drafts ---------------------------------------------------------------------------------
def _saved(rt: Runtime, draft_id: str, preview: str, conflicts: str) -> str:
    return (
        f"{preview}\n{conflicts}\n\nDraft saved as {draft_id}. Nothing has been written. "
        f"Ask the user to confirm; apply only with confirm_draft('{draft_id}', <exact reply>)."
    )


def draft_create_event_text(
    rt: Runtime,
    subject: str,
    when: str,
    duration_minutes: int | None = None,
    end: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    send_invitations: bool = False,
    calendar: str = "",
    link: str = "",
    reminder_minutes_before: int | None = DEFAULT_REMINDER_MINUTES,
    all_day: bool = False,
) -> str:
    def run() -> str:
        ensure_write_allowed(rt)
        now = _now(rt)
        title, place = clean_subject_and_location(subject, location)
        if not title:
            raise AgentError("An event needs a title. What should it be called?")
        _check_duration(duration_minutes)
        days = _all_day_span(rt, when, end, now=now, all_day=all_day)
        defaulted = False
        if days is not None:
            if duration_minutes:
                raise AgentError(
                    "An all-day event has no length in minutes. Give its last day as end instead."
                )
            start_dt, end_dt = _all_day_bounds(rt, *days)
            past_note = "\nNote: these days are in the past." if days[1] < now.date() else ""
        else:
            start_dt = resolve_moment(when, now=now, tz=rt.tz).astimezone(rt.tz)
            past_note = _past_start_note(rt, when, start_dt, now=now, title=title)
            end_dt = _resolve_end(rt, end, start_dt, now=now)
            if end_dt is None:
                # 0 is what a small model sends for "not given": same labelled default as
                # omitting it
                minutes = duration_minutes or DEFAULT_EVENT_MINUTES
                defaulted = not duration_minutes
                end_dt = start_dt + timedelta(minutes=minutes)
        if end_dt <= start_dt:
            raise AgentError("The end time must be after the start time.")
        people = parse_attendees(attendees)
        check_notification_policy(
            rt, has_attendees=bool(people), acknowledged=send_invitations, flag="send_invitations"
        )
        target = resolve_calendar(rt.calendar, calendar, for_write=True)
        try:
            draft = EventDraft(
                subject=title,
                start=start_dt,
                end=end_dt,
                location=place,
                duration_defaulted=defaulted,
                attendees=people,
                send_invitations=send_invitations,
                calendar_id=target.id,
                calendar_name=target.name,
                link=link or None,
                reminder_minutes_before=_reminder_arg(reminder_minutes_before),
                all_day=days is not None,
            )
        except ValueError as exc:
            raise AgentError(first_pydantic_message(exc)) from exc
        if draft.all_day:
            # a whole-day event sits above the day's appointments rather than clashing with them
            conflicts_text = "Conflicts: not checked for an all-day event."
        else:
            found = find_conflicts_everywhere(rt.calendar, draft.start, draft.end, target=target)
            conflicts_text = format_conflicts(found, rt.tz)
        saved = rt.drafts.save(draft)
        s = summary_for_create(draft)
        rt.audit.record(
            action="create",
            stage="proposed",
            draft_id=saved.id,
            subject=s.subject,
            start=s.start,
            end=s.end,
            extra={"calendar_id": s.calendar_id, "via": "mcp"},
        )
        return _saved(rt, saved.id, format_create_preview(draft, rt.tz), conflicts_text + past_note)

    return _guard(run)


def _all_day_span(
    rt: Runtime, when: str, end: str, *, now: datetime, all_day: bool
) -> tuple[date, date] | None:
    """The days a new event covers when it is an all-day event; None when it has times.

    An event is all-day when the caller says so (``all_day``), when the phrase says so ("7 nov
    all day", "full day 7 nov"), when ``when`` is a span of days ("7 nov to 19 nov"), or when
    ``end`` is a day rather than a time. A bare day on its own ("7 nov") is still an appointment
    with a missing time, and asking for the time is left to the caller.
    """
    when_days, flagged = strip_all_day(when)
    end_days, flagged_end = strip_all_day(end)
    wanted = all_day or flagged or flagged_end
    days = resolve_days(when_days, now=now, tz=rt.tz)
    if days is None:
        if wanted:
            raise DateAmbiguity(
                f"Which day or days is '{when}' for? For example '7 Nov' or '7 Nov to 19 Nov'. "
                "An all-day event has no start time."
            )
        return None
    first, last = days
    if end_days:
        ending = resolve_days(end_days, now=now, tz=rt.tz)
        if ending is None:
            if wanted:
                raise DateAmbiguity(
                    "An all-day event ends on a day, not at a time. Which day is the last one, "
                    "for example '19 Nov'?"
                )
            return None
        if ending[1] < first:
            raise DateAmbiguity(
                f"The last day ({ending[1]:%d %b}) is before the first ({first:%d %b}). "
                "Which days do you mean?"
            )
        last, wanted = ending[1], True
    if not wanted and first == last:
        return None
    return first, last


def _all_day_bounds(rt: Runtime, first: date, last: date) -> tuple[datetime, datetime]:
    """Midnight on the first day to midnight after the last, in the calendar's time zone."""
    start = datetime.combine(first, time.min, rt.tz)
    return start, datetime.combine(last + timedelta(days=1), time.min, rt.tz)


_DRAFT_ID_RE = re.compile(r"d-[0-9a-f]{6}")


def _reject_draft_id(event_id: str) -> None:
    """A model that wants to drop a waiting draft sometimes passes its id as an event id."""
    if _DRAFT_ID_RE.fullmatch(event_id.strip()):
        raise AgentError(
            f"{event_id} is a draft that is still waiting, not an event on the calendar. "
            "Call discard_draft to drop it."
        )


def _past_start_note(rt: Runtime, when: str, start: datetime, *, now: datetime, title: str) -> str:
    """Ask when a bare time has already passed; note it when the user named the day.

    "4pm" said at 6pm most likely means another day, so the question asks for the day (it is
    worded so that a plain yes is not an answer). A day given explicitly is taken as meant.
    """
    if start >= now - PAST_START_GRACE:
        return ""
    another_day = start.date() + timedelta(days=1)
    if resolve_moment(when, now=now, tz=rt.tz, default_day=another_day) != start:
        raise DateAmbiguity(
            f"{start:%H:%M} on {start:%A %d %b} has already passed. Which day do you mean for "
            f"'{title}'? For example 'tomorrow {start:%H:%M}'."
        )
    return "\nNote: this start time is in the past."


def _resolve_end(rt: Runtime, end: str, start: datetime, *, now: datetime) -> datetime | None:
    """Resolve an end phrase against the start. None means "no usable end was given".

    A time-only end lands on the start's day. If that is the start itself (a model echoing the
    start time as the end) it counts as not given; if it is earlier, the event runs past
    midnight ("11pm" to "1am"). An end that names its own day is taken as written.
    """
    if not end:
        return None
    day = start.date()
    resolved = resolve_moment(end, now=now, tz=rt.tz, default_day=day)
    if resolved == start:
        return None
    next_day = resolve_moment(end, now=now, tz=rt.tz, default_day=day + timedelta(days=1))
    if resolved < start and next_day != resolved:
        return next_day
    return resolved


def _check_duration(minutes: int | None) -> None:
    if minutes is not None and not 0 <= minutes <= MAX_EVENT_MINUTES:
        raise AgentError(
            f"duration_minutes must be between 1 and {MAX_EVENT_MINUTES}; "
            "for longer events give an end instead."
        )


def _amend_waiting_draft(
    rt: Runtime,
    draft_id: str,
    *,
    now: datetime,
    new_when: str,
    new_duration_minutes: int | None,
    new_end: str,
    new_subject: str,
    new_location: str,
    new_link: str | None,
    new_reminder_minutes_before: int | None,
) -> str:
    """Replace a waiting create-draft with a changed copy ("make it 2 hours").

    Nothing is written to the calendar: the old draft is discarded (and audited as such) and
    the copy is a new draft that needs its own confirmation.
    """
    old = rt.drafts.load(draft_id)
    payload = old.payload
    if not isinstance(payload, EventDraft):
        raise AgentError(
            f"{draft_id} is a waiting {old.kind} draft. To change it, call discard_draft for it "
            "and draft it again."
        )
    _check_duration(new_duration_minutes)
    start = payload.start.astimezone(rt.tz)
    end = payload.end.astimezone(rt.tz)
    length = payload.end - payload.start
    defaulted = payload.duration_defaulted
    past_note = ""
    if payload.all_day:
        if new_duration_minutes:
            raise AgentError(
                "An all-day event has no length in minutes. Give its last day as new_end instead."
            )
        moved_start, moved_end = _all_day_move(rt, start, end, new_when, new_end, now=now)
        if moved_start and moved_end:
            start = datetime.fromisoformat(moved_start)
            end = datetime.fromisoformat(moved_end)
    else:
        if new_when:
            # a time-only phrase ("3pm") stays on the draft's own day
            moment = resolve_moment(new_when, now=now, tz=rt.tz, default_day=start.date())
            start = moment.astimezone(rt.tz)
            past_note = _past_start_note(rt, new_when, start, now=now, title=payload.subject)
        end = start + length
        new_end_dt = _resolve_end(rt, new_end, start, now=now)
        if new_end_dt is not None:
            end = new_end_dt
            defaulted = False
        elif new_duration_minutes:
            end = start + timedelta(minutes=new_duration_minutes)
            defaulted = False
    if end <= start:
        raise AgentError("The end time must be after the start time.")
    # only text that is new gets cleaned; the title the user already saw stays as it is
    title, place = payload.subject, payload.location
    if new_subject:
        title, place = clean_subject_and_location(new_subject, new_location or payload.location)
    elif new_location:
        place = clean_subject_and_location(payload.subject, new_location)[1]
    changes: dict[str, object] = {
        "subject": title,
        "location": place,
        "start": start,
        "end": end,
        "duration_defaulted": defaulted,
    }
    if new_link is not None:
        changes["link"] = new_link or None
    if new_reminder_minutes_before is not None:
        changes["reminder_minutes_before"] = _reminder_arg(new_reminder_minutes_before)
    try:
        draft = EventDraft(**{**payload.model_dump(), **changes})
    except ValueError as exc:
        raise AgentError(first_pydantic_message(exc)) from exc
    target = resolve_calendar(rt.calendar, draft.calendar_id, for_write=True)
    conflicts = find_conflicts_everywhere(rt.calendar, draft.start, draft.end, target=target)
    saved = rt.drafts.save(draft)
    rt.drafts.discard(draft_id)
    rt.audit.record(
        action="create", stage="rejected", draft_id=draft_id, detail=f"replaced by {saved.id}"
    )
    summary = summary_for_create(draft)
    rt.audit.record(
        action="create",
        stage="proposed",
        draft_id=saved.id,
        subject=summary.subject,
        start=summary.start,
        end=summary.end,
        extra={"calendar_id": summary.calendar_id, "via": "mcp", "replaces": draft_id},
    )
    return (
        _saved(
            rt, saved.id, format_create_preview(draft, rt.tz), format_conflicts(conflicts, rt.tz)
        )
        + past_note
        + f"\nReplaces draft {draft_id}, which was discarded."
    )


def _all_day_move(
    rt: Runtime,
    start: datetime,
    end: datetime,
    new_when: str,
    new_end: str,
    *,
    now: datetime,
) -> tuple[str | None, str | None]:
    """New bounds for an all-day event from day phrases, as ISO text; (None, None) if unmoved.

    "12 nov" moves the first day and keeps the number of days; "12 nov to 14 nov" sets both;
    ``new_end`` names the new last day. Times are refused: an all-day event has none.
    """
    first = start.astimezone(rt.tz).date()
    last = (end.astimezone(rt.tz) - timedelta(days=1)).date()
    length = last - first
    if new_when:
        days = resolve_days(strip_all_day(new_when)[0], now=now, tz=rt.tz)
        if days is None:
            raise DateAmbiguity(
                f"This is an all-day event, so '{new_when}' needs a day rather than a time. "
                "Which day should it move to, for example '12 Nov'?"
            )
        first = days[0]
        last = days[1] if days[1] != days[0] else first + length
    if new_end:
        days = resolve_days(strip_all_day(new_end)[0], now=now, tz=rt.tz)
        if days is None:
            raise DateAmbiguity(
                "An all-day event ends on a day, not at a time. Which day is the last one, for "
                "example '19 Nov'?"
            )
        last = days[1]
        if last < first:
            raise DateAmbiguity(
                f"The last day ({last:%d %b}) is before the first ({first:%d %b}). Which days "
                "do you mean?"
            )
    if not new_when and not new_end:
        return None, None
    bounds = _all_day_bounds(rt, first, last)
    return bounds[0].isoformat(), bounds[1].isoformat()


def draft_update_event_text(
    rt: Runtime,
    find: str = "",
    on: str = "",
    event_id: str = "",
    new_when: str = "",
    new_duration_minutes: int | None = None,
    new_end: str = "",
    new_subject: str = "",
    new_location: str = "",
    notify_attendees: bool = False,
    calendar: str = "",
    new_link: str | None = None,
    new_reminder_minutes_before: int | None = None,
) -> str:
    def run() -> str:
        ensure_write_allowed(rt)
        now = _now(rt)
        if _DRAFT_ID_RE.fullmatch(event_id.strip()):
            return _amend_waiting_draft(
                rt,
                event_id.strip(),
                now=now,
                new_when=new_when,
                new_duration_minutes=new_duration_minutes,
                new_end=new_end,
                new_subject=new_subject,
                new_location=new_location,
                new_link=new_link,
                new_reminder_minutes_before=new_reminder_minutes_before,
            )
        target = resolve_calendar(rt.calendar, calendar, for_write=True)
        event = resolve_event(
            rt,
            event_id=event_id or None,
            find=find or None,
            on=None,
            days=14,
            calendar_id=target.id,
            window=_event_window(rt, on, now) if on else None,
        )
        if event.is_all_day:
            if new_duration_minutes:
                raise AgentError(
                    "An all-day event has no length in minutes. Give its last day as new_end "
                    "instead."
                )
            start_text, end_text = _all_day_move(
                rt, event.start, event.end, new_when, new_end, now=now
            )
        else:
            # time-only phrases ("3pm") stay on the event's own day: "move it to 3pm" keeps
            # the date
            anchor = event.start.astimezone(rt.tz).date()
            start_text = (
                resolve_moment(new_when, now=now, tz=rt.tz, default_day=anchor).isoformat()
                if new_when
                else None
            )
            end_anchor = (
                datetime.fromisoformat(start_text).astimezone(rt.tz).date()
                if start_text
                else anchor
            )
            end_text = (
                resolve_moment(new_end, now=now, tz=rt.tz, default_day=end_anchor).isoformat()
                if new_end
                else None
            )
        draft = build_update_draft(
            rt,
            event,
            subject=new_subject or None,
            start_text=start_text,
            end_text=end_text,
            duration_minutes=new_duration_minutes,
            location=new_location or None,
            notify_attendees=notify_attendees,
            calendar_name=target.name,
            link=new_link,
            reminder_minutes_before=(
                _reminder_arg(new_reminder_minutes_before)
                if new_reminder_minutes_before is not None and new_reminder_minutes_before >= 0
                else None
            ),
            use_default_reminder=(
                new_reminder_minutes_before is not None and new_reminder_minutes_before < 0
            ),
        )
        conflicts = find_conflicts_everywhere(
            rt.calendar,
            draft.effective_start,
            draft.effective_end,
            exclude_id=event.id,
            target=target,
        )
        saved = rt.drafts.save(draft)
        s = summary_for_update(draft)
        rt.audit.record(
            action="update",
            stage="proposed",
            draft_id=saved.id,
            event_id=s.event_id,
            subject=s.subject,
            start=s.start,
            end=s.end,
            extra={"calendar_id": s.calendar_id, "via": "mcp"},
        )
        return _saved(
            rt, saved.id, format_update_preview(draft, rt.tz), format_conflicts(conflicts, rt.tz)
        )

    return _guard(run)


def draft_delete_event_text(
    rt: Runtime,
    find: str = "",
    on: str = "",
    event_id: str = "",
    notify_attendees: bool = False,
    calendar: str = "",
) -> str:
    def run() -> str:
        ensure_write_allowed(rt)
        _reject_draft_id(event_id)
        now = _now(rt)
        target = resolve_calendar(rt.calendar, calendar, for_write=True)
        event = resolve_event(
            rt,
            event_id=event_id or None,
            find=find or None,
            on=None,
            days=14,
            calendar_id=target.id,
            window=_event_window(rt, on, now) if on else None,
        )
        draft = build_delete_draft(
            rt, event, notify_attendees=notify_attendees, calendar_name=target.name
        )
        saved = rt.drafts.save(draft)
        s = summary_for_delete(draft)
        rt.audit.record(
            action="delete",
            stage="proposed",
            draft_id=saved.id,
            event_id=s.event_id,
            subject=s.subject,
            start=s.start,
            end=s.end,
            extra={"calendar_id": s.calendar_id, "via": "mcp"},
        )
        return _saved(
            rt,
            saved.id,
            format_delete_preview(draft, rt.tz),
            "This deletion is permanent.",
        )

    return _guard(run)


def list_drafts_text(rt: Runtime) -> str:
    drafts = rt.drafts.list()
    if not drafts:
        return "No pending drafts."
    return "Pending drafts:\n" + "\n".join(
        f"  {format_draft_line(d.id, d.payload, rt.tz)}" for d in drafts
    )


def discard_draft_text(rt: Runtime, draft_id: str) -> str:
    def run() -> str:
        ids = draft_ids(draft_id)
        drafts = [rt.drafts.load(one) for one in ids]  # all must exist before any is discarded
        for draft in drafts:
            rt.drafts.discard(draft.id)
            rt.audit.record(
                action=draft.kind,  # type: ignore[arg-type]
                stage="rejected",
                draft_id=draft.id,
                detail="discarded",
            )
        if len(drafts) == 1:
            return f"Draft {drafts[0].id} discarded. Nothing was changed."
        return f"{len(drafts)} drafts discarded ({', '.join(ids)}). Nothing was changed."

    return _guard(run)


def confirm_draft_text(rt: Runtime, draft_id: str, user_reply: str) -> str:
    """Apply the draft(s) only if ``user_reply`` is the single word ``yes``; else discard them.

    ``draft_id`` may name several drafts (see ``draft_ids``): a batch the user approves once.
    Each is applied on its own, in order, and the reply has one numbered line per draft so a
    failure in one is visible next to the others that went through."""

    def run() -> str:
        ids = draft_ids(draft_id)
        for one in ids:
            rt.drafts.load(one)  # every id must be known before anything is applied
        if len(ids) == 1:
            return _confirm_one(rt, ids[0], user_reply)
        results = [_guard(partial(_confirm_one, rt, one, user_reply)) for one in ids]
        return "\n".join(f"{n}. {text}" for n, text in enumerate(results, 1))

    return _guard(run)


def _confirm_one(rt: Runtime, draft_id: str, user_reply: str) -> str:
    def run() -> str:
        ensure_write_allowed(rt)
        draft = rt.drafts.load(draft_id)
        payload = draft.payload
        if not is_exact_confirmation(user_reply):
            if _is_rejection(user_reply):
                rt.drafts.discard(draft_id)
                rt.audit.record(
                    action=draft.kind,  # type: ignore[arg-type]
                    stage="rejected",
                    draft_id=draft_id,
                    detail=f"reply was not 'yes': {user_reply[:40]!r}",
                )
                return f"Not confirmed. Draft {draft_id} discarded; nothing was changed."
            return (
                f"Not confirmed. Draft {draft_id} is kept; reply yes to apply it or cancel to "
                "discard it."
            )

        silent: list[str] = []
        reader = lambda _prompt: "yes"  # noqa: E731 - the literal 'yes' was verified above
        try:
            if isinstance(payload, EventDraft):
                created = confirm_and_execute(
                    rt,
                    summary=summary_for_create(payload),
                    draft_id=draft_id,
                    question="Create this event?",
                    perform=lambda: rt.calendar.create_event(payload),
                    echo=silent.append,
                    reader=reader,
                )
                rt.drafts.discard(draft_id)
                return (
                    f"Created: {created.subject} {format_dt(created.start, rt.tz)} "
                    f"(event id {created.id})."
                )
            if isinstance(payload, UpdateDraft):
                verify_unchanged(rt, payload.original)
                updated = confirm_and_execute(
                    rt,
                    summary=summary_for_update(payload),
                    draft_id=draft_id,
                    question="Update this event?",
                    perform=lambda: rt.calendar.update_event(payload),
                    echo=silent.append,
                    reader=reader,
                )
                rt.drafts.discard(draft_id)
                return f"Updated: {updated.subject} now {format_dt(updated.start, rt.tz)}."
            if isinstance(payload, DeleteDraft):
                verify_unchanged(rt, payload.original)
                confirm_and_execute(
                    rt,
                    summary=summary_for_delete(payload),
                    draft_id=draft_id,
                    question="Delete this event permanently?",
                    perform=lambda: rt.calendar.delete_event(payload),
                    echo=silent.append,
                    reader=reader,
                )
                rt.drafts.discard(draft_id)
                o = payload.original
                return f"Deleted: {o.subject} {format_dt(o.start, rt.tz)}."
        except UserCancelledError:  # cannot happen with a verified 'yes', kept for safety
            return f"Not confirmed. Draft {draft_id} kept."
        raise AgentError(f"Unknown draft kind for {draft_id}.")

    return _guard(run)
