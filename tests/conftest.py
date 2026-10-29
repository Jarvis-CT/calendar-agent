"""Shared fixtures: recording fake API clients and Runtimes wired to them (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from outlook_calendar_agent import runtime as runtime_module
from outlook_calendar_agent.audit import AuditLog
from outlook_calendar_agent.config import GoogleSettings, MicrosoftSettings, Settings
from outlook_calendar_agent.drafts import DraftStore
from outlook_calendar_agent.errors import NotFoundError
from outlook_calendar_agent.google_calendar import GoogleCalendarService
from outlook_calendar_agent.graph_calendar import GraphCalendarService
from outlook_calendar_agent.runtime import Runtime
from outlook_calendar_agent.timeutil import SGT

EVENT_ID = "AAMkAGI2TG93AAA="
GOOGLE_EVENT_ID = "abc123def456"


# ---- Microsoft Graph fixtures ------------------------------------------------------------------
def graph_event(
    event_id: str = EVENT_ID,
    subject: str = "Project review",
    start: str = "2026-10-07T14:00:00.0000000",
    end: str = "2026-10-07T15:00:00.0000000",
    *,
    tz: str = "Asia/Singapore",
    event_type: str = "singleInstance",
    attendees: list[dict[str, Any]] | None = None,
    is_organizer: bool = True,
    change_key: str = "ck-1",
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        "isAllDay": False,
        "location": {"displayName": "Room 4"},
        "organizer": {"emailAddress": {"name": "Chia Geng", "address": "me@example.com"}},
        "isCancelled": False,
        "isOrganizer": is_organizer,
        "type": event_type,
        "attendees": attendees or [],
        "changeKey": change_key,
    }
    data.update(extra)
    return data


@dataclass
class Call:
    method: str
    path: str
    params: dict[str, str] | None = None
    json: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


@dataclass
class FakeGraphClient:
    """Records every call; mutating calls can be redirected through handlers."""

    calls: list[Call] = field(default_factory=list)
    calendar_view: list[dict[str, Any]] = field(default_factory=list)
    events_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    me: dict[str, Any] = field(
        default_factory=lambda: {"displayName": "Chia Geng", "mail": "me@example.com"}
    )
    post_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def write_calls(self) -> list[Call]:
        return [c for c in self.calls if c.method in {"POST", "PATCH", "DELETE"}]

    def _record(self, method: str, path: str, **kwargs: Any) -> None:
        self.calls.append(Call(method, path, **kwargs))

    def get(self, path: str, *, params=None, headers=None) -> dict[str, Any]:
        self._record("GET", path, params=dict(params or {}), headers=dict(headers or {}))
        if path == "/me":
            return self.me
        if "/events/" in path:
            key = unquote(path.rsplit("/events/", 1)[1])
            if key not in self.events_by_id:
                raise NotFoundError("Resource not found (404 ErrorItemNotFound)", status=404)
            return self.events_by_id[key]
        raise AssertionError(f"unexpected GET {path}")

    def get_all(self, path: str, *, params=None, headers=None) -> list[dict[str, Any]]:
        self._record("GET", path, params=dict(params or {}), headers=dict(headers or {}))
        if path == "/me/calendars":
            return [{"id": "cal-1", "name": "Calendar", "isDefaultCalendar": True, "canEdit": True}]
        assert path.endswith("/calendarView"), path
        return list(self.calendar_view)

    def post(self, path: str, *, json: dict[str, Any], params=None, headers=None):
        self._record("POST", path, json=json, headers=dict(headers or {}))
        if self.post_handler:
            return self.post_handler(json)
        return graph_event(
            event_id="NEW-ID",
            subject=json["subject"],
            start=json["start"]["dateTime"],
            end=json["end"]["dateTime"],
        )

    def patch(self, path: str, *, json: dict[str, Any], params=None, headers=None):
        self._record("PATCH", path, json=json, headers=dict(headers or {}))
        current = dict(self.events_by_id[unquote(path.rsplit("/events/", 1)[1])])
        current.update(json)
        return current

    def delete(self, path: str, *, params=None, headers=None) -> None:
        self._record("DELETE", path, headers=dict(headers or {}))


# ---- Google fixtures --------------------------------------------------------------------
def google_event(
    event_id: str = GOOGLE_EVENT_ID,
    summary: str = "Project review",
    start: str = "2026-10-07T14:00:00+08:00",
    end: str = "2026-10-07T15:00:00+08:00",
    *,
    attendees: list[dict[str, Any]] | None = None,
    is_organizer: bool = True,
    etag: str = '"etag-1"',
    **extra: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start, "timeZone": "Asia/Singapore"},
        "end": {"dateTime": end, "timeZone": "Asia/Singapore"},
        "location": "Room 4",
        "organizer": {"email": "me@example.com", "displayName": "Chia Geng", "self": is_organizer},
        "status": "confirmed",
        "attendees": attendees or [],
        "etag": etag,
        "htmlLink": "https://calendar.google.com/event?eid=x",
    }
    data.update(extra)
    return data


@dataclass
class FakeGoogleClient:
    calls: list[Call] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    events_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    post_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def write_calls(self) -> list[Call]:
        return [c for c in self.calls if c.method in {"POST", "PATCH", "DELETE"}]

    def _record(self, method: str, path: str, **kwargs: Any) -> None:
        self.calls.append(Call(method, path, **kwargs))

    calendars: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "id": "me@example.com",
                "summary": "me@example.com",
                "primary": True,
                "accessRole": "owner",
            },
            {"id": "work123@group.calendar.google.com", "summary": "Work", "accessRole": "writer"},
            {"id": "fam456@group.calendar.google.com", "summary": "Family", "accessRole": "reader"},
        ]
    )

    def get(self, path: str, *, params=None, headers=None) -> dict[str, Any]:
        self._record("GET", path, params=dict(params or {}), headers=dict(headers or {}))
        key = unquote(path.rsplit("/events/", 1)[1])
        if key not in self.events_by_id:
            raise NotFoundError("Event not found (404 notFound)", status=404)
        return self.events_by_id[key]

    def get_all(self, path: str, *, params=None, headers=None) -> list[dict[str, Any]]:
        self._record("GET", path, params=dict(params or {}), headers=dict(headers or {}))
        if path == "/users/me/calendarList":
            return list(self.calendars)
        assert path.startswith("/calendars/") and path.endswith("/events"), path
        return list(self.events)

    def post(self, path: str, *, json: dict[str, Any], params=None, headers=None):
        self._record(
            "POST", path, json=json, params=dict(params or {}), headers=dict(headers or {})
        )
        if self.post_handler:
            return self.post_handler(json)
        if "date" in json["start"]:  # an all-day event comes back with dates, not times
            created = google_event(event_id="NEW-ID", summary=json["summary"])
            created["start"], created["end"] = dict(json["start"]), dict(json["end"])
            return created
        return google_event(
            event_id="NEW-ID",
            summary=json["summary"],
            start=json["start"]["dateTime"],
            end=json["end"]["dateTime"],
        )

    def patch(self, path: str, *, json: dict[str, Any], params=None, headers=None):
        self._record(
            "PATCH", path, json=json, params=dict(params or {}), headers=dict(headers or {})
        )
        current = dict(self.events_by_id[unquote(path.rsplit("/events/", 1)[1])])
        current.update(json)
        return current

    def delete(self, path: str, *, params=None, headers=None) -> None:
        self._record("DELETE", path, params=dict(params or {}), headers=dict(headers or {}))


class FakeAuth:
    """Stands in for both authenticators. Never returns anything resembling a real token."""

    def __init__(self) -> None:
        self.logged_in = False

    def acquire_token(self) -> str:
        return "not-a-real-token"

    def login(self, echo, **kwargs) -> dict[str, Any]:
        echo("Open this URL ... (fake)")
        self.logged_in = True
        return {
            "name": "Chia Geng",
            "email": "me@example.com",
            "preferred_username": "me@example.com",
        }

    def logout(self) -> int:
        return 1

    def signed_in_email(self) -> str | None:
        return "me@example.com"

    def userinfo(self) -> dict[str, Any]:
        return {"name": "Chia Geng", "email": "me@example.com"}


# ---- runtime builders ---------------------------------------------------------------------------
def make_settings(tmp_path: Path, *, provider: str = "google", write: bool = True) -> Settings:
    google = None
    microsoft = None
    if provider == "google":
        google = GoogleSettings(
            client_id="123456-abcdef.apps.googleusercontent.com",
            client_secret="test-secret-not-real",
            scopes=("calendar.events",) if write else ("calendar.events.readonly",),
        )
    else:
        microsoft = MicrosoftSettings(
            client_id="11111111-2222-3333-4444-555555555555",
            tenant_id="common",
            scopes=("User.Read", "Calendars.ReadWrite" if write else "Calendars.Read"),
        )
    return Settings(
        provider=provider,  # type: ignore[arg-type]
        timezone_name="Asia/Singapore",
        state_dir=tmp_path / "state",
        google=google,
        microsoft=microsoft,
    )


def make_runtime(
    tmp_path: Path, client: Any, *, provider: str = "google", write: bool = True
) -> Runtime:
    settings = make_settings(tmp_path, provider=provider, write=write)
    auth = FakeAuth()
    if provider == "google":
        calendar = GoogleCalendarService(client, auth, SGT)  # type: ignore[arg-type]
    else:
        calendar = GraphCalendarService(client, SGT)  # type: ignore[arg-type]
    return Runtime(
        settings=settings,
        auth=auth,  # type: ignore[arg-type]
        calendar=calendar,
        audit=AuditLog(settings.audit_log_path),
        drafts=DraftStore(settings.drafts_dir),
    )


@pytest.fixture
def graph() -> FakeGraphClient:
    client = FakeGraphClient()
    client.events_by_id[EVENT_ID] = graph_event()
    client.calendar_view = [graph_event()]
    return client


@pytest.fixture
def google() -> FakeGoogleClient:
    client = FakeGoogleClient()
    client.events_by_id[GOOGLE_EVENT_ID] = google_event()
    client.events = [google_event()]
    return client


@pytest.fixture
def rt(tmp_path: Path, graph: FakeGraphClient, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Microsoft-backed runtime (the original write-command tests run against it)."""
    runtime = make_runtime(tmp_path, graph, provider="microsoft")
    monkeypatch.setattr(runtime_module, "get_runtime", lambda: runtime)
    return runtime


@pytest.fixture
def grt(tmp_path: Path, google: FakeGoogleClient, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Google-backed runtime."""
    runtime = make_runtime(tmp_path, google, provider="google")
    monkeypatch.setattr(runtime_module, "get_runtime", lambda: runtime)
    return runtime


def _read_audit(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.fixture
def audit_entries(rt: Runtime) -> Callable[[], list[dict[str, Any]]]:
    return lambda: _read_audit(rt.settings.audit_log_path)


@pytest.fixture
def google_audit_entries(grt: Runtime) -> Callable[[], list[dict[str, Any]]]:
    return lambda: _read_audit(grt.settings.audit_log_path)
