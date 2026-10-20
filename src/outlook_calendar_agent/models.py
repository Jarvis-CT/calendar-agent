"""Pydantic models: parsed calendar events and the draft (proposal) models for writes.

Draft models build the exact Graph payloads. They never set ``isOnlineMeeting``,
``recurrence`` or ``organizer``; those are intentionally deferred.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .timeutil import (
    SGT,
    parse_google_datetime,
    parse_graph_datetime,
    to_google_date,
    to_google_datetime,
    to_graph_datetime,
)

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+?(?=[.,;:!?]*(?:\s|$|<|\"|'|\)))")


def extract_links(*sources: str | None) -> list[str]:
    """URLs found in the given texts, in order, de-duplicated, without trailing punctuation."""
    found: list[str] = []
    for source in sources:
        for url in _URL_RE.findall(source or ""):
            url = url.rstrip(".,;:!?")
            if url and url not in found:
                found.append(url)
    return found


LINK_PREFIX = "Meeting link: "
DEFAULT_REMINDER_MINUTES = 24 * 60  # one day before, per the user's preference
REMINDER_OFF = -1  # sentinel: no reminder at all (None means "provider default")
_NAMED_EMAIL_RE = re.compile(r"^\s*(?P<name>[^<>]*?)\s*<(?P<email>[^<>]+)>\s*$")

# Small projections for Graph $select. Listing uses the minimal set; details add write context.
LIST_SELECT: tuple[str, ...] = (
    "id",
    "subject",
    "start",
    "end",
    "isAllDay",
    "location",
    "organizer",
    "isCancelled",
    "type",
)
DETAIL_SELECT: tuple[str, ...] = LIST_SELECT + (
    "isOrganizer",
    "attendees",
    "onlineMeeting",
    "isOnlineMeeting",
    "webLink",
    "changeKey",
    "body",
    "isReminderOn",
    "reminderMinutesBeforeStart",
)

EventType = Literal["singleInstance", "occurrence", "exception", "seriesMaster"]


class Attendee(BaseModel):
    email: str
    name: str | None = None
    type: Literal["required", "optional"] = "required"

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        value = value.strip()
        if not _EMAIL_RE.match(value):
            raise ValueError(f"{value!r} is not a valid e-mail address")
        return value

    @classmethod
    def parse(cls, text: str, *, type: Literal["required", "optional"] = "required") -> Attendee:
        """Accept ``alice@example.com`` or ``Alice Tan <alice@example.com>``."""
        match = _NAMED_EMAIL_RE.match(text)
        if match:
            name = match.group("name").strip().strip('"') or None
            return cls(email=match.group("email"), name=name, type=type)
        return cls(email=text, type=type)

    def display(self) -> str:
        label = f"{self.name} <{self.email}>" if self.name else self.email
        return f"{label} ({self.type})"

    def to_graph(self) -> dict[str, Any]:
        address: dict[str, str] = {"address": self.email}
        if self.name:
            address["name"] = self.name
        return {"emailAddress": address, "type": self.type}

    def to_google(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"email": self.email}
        if self.name:
            entry["displayName"] = self.name
        if self.type == "optional":
            entry["optional"] = True
        return entry


class CalendarInfo(BaseModel):
    """One calendar visible to the signed-in account."""

    id: str
    name: str
    is_primary: bool = False
    can_write: bool = False

    def display(self) -> str:
        flags = [f for f, on in (("primary", self.is_primary), ("writable", self.can_write)) if on]
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        return f"{self.name}{suffix}\n    id: {self.id}"


class TaskList(BaseModel):
    id: str
    name: str


class TaskItem(BaseModel):
    """A Google Task (read-only). Tasks have a due *date*, not a time."""

    id: str
    title: str
    due: date | None = None
    completed: bool = False
    notes: str | None = None
    list_name: str = "My Tasks"
    in_default_list: bool = True


class CalendarEvent(BaseModel):
    id: str
    calendar_id: str = "primary"
    subject: str = "(no subject)"
    start: datetime
    end: datetime
    is_all_day: bool = False
    location: str | None = None
    organizer_name: str | None = None
    organizer_email: str | None = None
    is_cancelled: bool = False
    is_organizer: bool | None = None
    event_type: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    online_join_url: str | None = None
    web_link: str | None = None
    change_key: str | None = None
    description: str | None = None
    description_is_html: bool = False
    reminder_minutes_before: int | None = None  # None = calendar default, REMINDER_OFF = none

    @property
    def links(self) -> list[str]:
        """Meeting/other URLs: the provider's join link first, then any URL in location/notes."""
        return extract_links(self.online_join_url, self.location, self.description)

    @classmethod
    def from_graph(cls, data: dict[str, Any], *, calendar_id: str = "primary") -> CalendarEvent:
        organizer = (data.get("organizer") or {}).get("emailAddress") or {}
        location = (data.get("location") or {}).get("displayName") or None
        online = data.get("onlineMeeting") or {}
        attendees: list[Attendee] = []
        for raw in data.get("attendees") or []:
            address = (raw.get("emailAddress") or {}).get("address")
            if not address or not _EMAIL_RE.match(address):
                continue
            kind = raw.get("type") if raw.get("type") in {"required", "optional"} else "required"
            attendees.append(
                Attendee(email=address, name=(raw.get("emailAddress") or {}).get("name"), type=kind)
            )
        return cls(
            id=data["id"],
            calendar_id=calendar_id,
            subject=data.get("subject") or "(no subject)",
            start=parse_graph_datetime(data["start"]),
            end=parse_graph_datetime(data["end"]),
            is_all_day=bool(data.get("isAllDay", False)),
            location=location,
            organizer_name=organizer.get("name"),
            organizer_email=organizer.get("address"),
            is_cancelled=bool(data.get("isCancelled", False)),
            is_organizer=data.get("isOrganizer"),
            event_type=data.get("type"),
            attendees=attendees,
            online_join_url=online.get("joinUrl"),
            web_link=data.get("webLink"),
            change_key=data.get("changeKey"),
            description=(
                (data.get("body") or {}).get("content") or data.get("bodyPreview") or None
            ),
            reminder_minutes_before=(
                data.get("reminderMinutesBeforeStart")
                if data.get("isReminderOn", True)
                else REMINDER_OFF
            ),
            description_is_html=(
                str((data.get("body") or {}).get("contentType") or "").lower() == "html"
            ),
        )

    @classmethod
    def from_google(
        cls, data: dict[str, Any], tz: Any = SGT, *, calendar_id: str = "primary"
    ) -> CalendarEvent:
        organizer = data.get("organizer") or {}
        attendees: list[Attendee] = []
        for raw in data.get("attendees") or []:
            address = raw.get("email")
            if not address or raw.get("organizer") or not _EMAIL_RE.match(address):
                continue
            attendees.append(
                Attendee(
                    email=address,
                    name=raw.get("displayName"),
                    type="optional" if raw.get("optional") else "required",
                )
            )
        if data.get("recurringEventId"):
            event_type = "occurrence"
        elif data.get("recurrence"):
            event_type = "seriesMaster"
        else:
            event_type = "singleInstance"
        start_raw, end_raw = data.get("start") or {}, data.get("end") or {}
        is_all_day = "date" in start_raw and "dateTime" not in start_raw
        return cls(
            id=data["id"],
            calendar_id=calendar_id,
            subject=data.get("summary") or "(no subject)",
            start=parse_google_datetime(start_raw, tz),
            end=parse_google_datetime(end_raw, tz),
            is_all_day=is_all_day,
            location=data.get("location") or None,
            organizer_name=organizer.get("displayName"),
            organizer_email=organizer.get("email"),
            is_cancelled=data.get("status") == "cancelled",
            is_organizer=organizer.get("self") if organizer else None,
            event_type=event_type,
            attendees=attendees,
            online_join_url=data.get("hangoutLink"),
            web_link=data.get("htmlLink"),
            change_key=data.get("etag"),
            description=data.get("description") or None,
            reminder_minutes_before=_google_reminder(data.get("reminders")),
        )

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start


def _google_reminder(reminders: dict[str, Any] | None) -> int | None:
    if not reminders or reminders.get("useDefault", True):
        return None
    popups = [
        o.get("minutes") for o in reminders.get("overrides") or [] if o.get("minutes") is not None
    ]
    return min(popups) if popups else REMINDER_OFF  # overrides present but empty = no reminder


_LINK_LINE_RE = re.compile(rf"(?:<p>\s*)?{re.escape(LINK_PREFIX)}\S+(?:\s*</p>)?", re.IGNORECASE)


def with_link(description: str | None, link: str | None, *, html: bool = False) -> str | None:
    """Return the description with exactly one 'Meeting link:' entry (replaced if present).

    Works for plain text and for HTML bodies (Google descriptions are often single-line HTML).
    """
    text = _LINK_LINE_RE.sub("", description or "").strip()
    if link:
        entry = f"<p>{LINK_PREFIX}{link}</p>" if html else f"{LINK_PREFIX}{link}"
        text = f"{text}\n{entry}".strip() if text else entry
    return text or None


class EventSnapshot(BaseModel):
    """What an existing event looked like when a draft was prepared."""

    id: str
    calendar_id: str = "primary"
    calendar_name: str | None = None
    subject: str
    start: datetime
    end: datetime
    location: str | None = None
    description: str | None = None
    description_is_html: bool = False
    online_join_url: str | None = None
    reminder_minutes_before: int | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    is_organizer: bool | None = None
    event_type: str | None = None
    change_key: str | None = None
    is_all_day: bool = False

    @classmethod
    def from_event(cls, event: CalendarEvent, *, calendar_name: str | None = None) -> EventSnapshot:
        data = event.model_dump(include=set(cls.model_fields) - {"calendar_name"})
        return cls(**data, calendar_name=calendar_name)

    @property
    def calendar_label(self) -> str:
        return _calendar_label(self.calendar_id, self.calendar_name)

    @property
    def calendar_info(self) -> CalendarInfo:
        return CalendarInfo(
            id=self.calendar_id,
            name=self.calendar_name or self.calendar_id,
            is_primary=self.calendar_id == "primary",
            can_write=True,
        )

    @property
    def links(self) -> list[str]:
        return extract_links(self.online_join_url, self.location, self.description)

    def matches(self, event: CalendarEvent) -> bool:
        if self.change_key and event.change_key:
            return self.change_key == event.change_key
        return self.subject == event.subject and self.start == event.start and self.end == event.end


def _calendar_label(calendar_id: str, calendar_name: str | None) -> str:
    if calendar_name and calendar_name != calendar_id:
        return f"{calendar_name} ({calendar_id})"
    return calendar_id


def _require_aware(name: str, value: datetime | None) -> None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must include a time zone")


class EventDraft(BaseModel):
    """Proposal for creating an event (Google ``events.insert`` / Graph ``POST /me/events``)."""

    kind: Literal["create"] = "create"
    subject: str = Field(min_length=1)
    start: datetime
    end: datetime
    location: str | None = None
    body: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    send_invitations: bool = False
    calendar_id: str = "primary"
    calendar_name: str | None = None
    link: str | None = None  # meeting URL, stored in the description; never auto-generated
    reminder_minutes_before: int | None = DEFAULT_REMINDER_MINUTES  # None = calendar default
    duration_defaulted: bool = False  # True when the user gave no length and the default applied
    # Whole days, no times: start is midnight on the first day, end midnight after the last.
    all_day: bool = False

    @property
    def calendar_label(self) -> str:
        return _calendar_label(self.calendar_id, self.calendar_name)

    @property
    def calendar_info(self) -> CalendarInfo:
        return CalendarInfo(
            id=self.calendar_id,
            name=self.calendar_name or self.calendar_id,
            is_primary=self.calendar_id == "primary",
            can_write=True,
        )

    @property
    def description(self) -> str | None:
        return with_link(self.body, self.link)

    @property
    def last_day(self) -> date:
        """The last day an all-day event covers (its end is the midnight after it)."""
        return (self.end - timedelta(days=1)).date()

    @field_validator("link")
    @classmethod
    def _check_link(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _URL_RE.fullmatch(value):
            raise ValueError(f"{value!r} is not a valid http(s) link")
        return value

    @model_validator(mode="after")
    def _validate(self) -> EventDraft:
        _require_aware("start", self.start)
        _require_aware("end", self.end)
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.all_day and (self.start.time() != time.min or self.end.time() != time.min):
            raise ValueError("an all-day event starts and ends at midnight")
        return self

    @property
    def invitations_will_be_sent(self) -> bool:
        return bool(self.attendees) and self.send_invitations

    def to_graph_payload(self, tz: tzinfo = SGT) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": self.subject,
            "start": to_graph_datetime(self.start, tz),
            "end": to_graph_datetime(self.end, tz),
        }
        if self.all_day:
            payload["isAllDay"] = True
        if self.description:
            payload["body"] = {"contentType": "text", "content": self.description}
        if self.location:
            payload["location"] = {"displayName": self.location}
        if self.attendees:
            payload["attendees"] = [a.to_graph() for a in self.attendees]
        payload.update(_graph_reminder_fields(self.reminder_minutes_before))
        return payload

    def to_google_payload(self, tz: tzinfo = SGT) -> dict[str, Any]:
        stamp = to_google_date if self.all_day else to_google_datetime
        payload: dict[str, Any] = {
            "summary": self.subject,
            "start": stamp(self.start, tz),
            "end": stamp(self.end, tz),
        }
        if self.description:
            payload["description"] = self.description
        if self.location:
            payload["location"] = self.location
        if self.attendees:
            payload["attendees"] = [a.to_google() for a in self.attendees]
        if self.reminder_minutes_before is not None:
            payload["reminders"] = _google_reminder_payload(self.reminder_minutes_before)
        return payload


def _google_reminder_payload(reminder: int | None) -> dict[str, Any]:
    if reminder is None:
        return {"useDefault": True}
    if reminder == REMINDER_OFF:
        return {"useDefault": False, "overrides": []}
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder}]}


def _graph_reminder_fields(reminder: int | None) -> dict[str, Any]:
    if reminder is None:
        return {}  # provider default
    if reminder == REMINDER_OFF:
        return {"isReminderOn": False}
    return {"isReminderOn": True, "reminderMinutesBeforeStart": reminder}


class UpdateDraft(BaseModel):
    """Proposal for a partial update (Google ``events.patch`` / Graph ``PATCH``)."""

    kind: Literal["update"] = "update"
    original: EventSnapshot
    subject: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    location: str | None = None
    link: str | None = None  # set a meeting link ("" clears it)
    reminder_minutes_before: int | None = None  # minutes, REMINDER_OFF, or None = keep as is
    use_default_reminder: bool = False  # True = switch back to the calendar default
    notify_attendees: bool = False

    @field_validator("link")
    @classmethod
    def _check_link(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value and not _URL_RE.fullmatch(value):
            raise ValueError(f"{value!r} is not a valid http(s) link")
        return value

    @model_validator(mode="after")
    def _validate(self) -> UpdateDraft:
        _require_aware("start", self.start)
        _require_aware("end", self.end)
        if not self.changed_fields():
            raise ValueError("no changes requested")
        if self.effective_end <= self.effective_start:
            raise ValueError("end must be after start")
        return self

    @property
    def effective_start(self) -> datetime:
        return self.start or self.original.start

    @property
    def effective_end(self) -> datetime:
        return self.end or self.original.end

    @property
    def notices_will_be_sent(self) -> bool:
        return bool(self.original.attendees) and self.notify_attendees

    def changed_fields(self) -> list[str]:
        changed = []
        if self.subject is not None and self.subject != self.original.subject:
            changed.append("subject")
        if self.start is not None and self.start != self.original.start:
            changed.append("start")
        if self.end is not None and self.end != self.original.end:
            changed.append("end")
        if self.location is not None and self.location != (self.original.location or ""):
            changed.append("location")
        if self.link is not None and self.new_description != self.original.description:
            changed.append("link")
        if (
            self.reminder_minutes_before is not None or self.use_default_reminder
        ) and self.effective_reminder != self.original.reminder_minutes_before:
            changed.append("reminder")
        return changed

    @property
    def new_description(self) -> str | None:
        return with_link(
            self.original.description, self.link or None, html=self.original.description_is_html
        )

    @property
    def effective_reminder(self) -> int | None:
        if self.use_default_reminder:
            return None
        if self.reminder_minutes_before is None:
            return self.original.reminder_minutes_before
        return self.reminder_minutes_before

    def to_graph_patch(self, tz: tzinfo = SGT) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        changed = self.changed_fields()
        if "subject" in changed:
            patch["subject"] = self.subject
        if "start" in changed or "end" in changed:
            # Always send both so the server never has to infer one from the other.
            patch["start"] = to_graph_datetime(self.effective_start, tz)
            patch["end"] = to_graph_datetime(self.effective_end, tz)
        if "location" in changed:
            patch["location"] = {"displayName": self.location}
        if "link" in changed:
            patch["body"] = {
                "contentType": "html" if self.original.description_is_html else "text",
                "content": self.new_description or "",
            }
        if "reminder" in changed:
            patch.update(_graph_reminder_fields(self.effective_reminder) or {"isReminderOn": True})
        return patch

    def to_google_patch(self, tz: tzinfo = SGT) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        changed = self.changed_fields()
        if "subject" in changed:
            patch["summary"] = self.subject
        if "start" in changed or "end" in changed:
            # an all-day event stays all-day: its days move, it gains no times
            stamp = to_google_date if self.original.is_all_day else to_google_datetime
            patch["start"] = stamp(self.effective_start, tz)
            patch["end"] = stamp(self.effective_end, tz)
        if "location" in changed:
            patch["location"] = self.location
        if "link" in changed:
            patch["description"] = self.new_description or ""
        if "reminder" in changed:
            patch["reminders"] = _google_reminder_payload(self.effective_reminder)
        return patch


class DeleteDraft(BaseModel):
    """Proposal for deleting an event (Google ``events.delete`` / Graph ``DELETE``)."""

    kind: Literal["delete"] = "delete"
    original: EventSnapshot
    notify_attendees: bool = False

    @property
    def cancellations_will_be_sent(self) -> bool:
        return bool(self.original.attendees) and self.notify_attendees


DraftPayload = EventDraft | UpdateDraft | DeleteDraft


class Draft(BaseModel):
    id: str
    created_at: datetime
    payload: DraftPayload = Field(discriminator="kind")

    @property
    def kind(self) -> str:
        return self.payload.kind
