"""All-day events: whole days with no times, single or spanning several days."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from outlook_calendar_agent import agent_api
from outlook_calendar_agent.dates import DateAmbiguity, resolve_days, strip_all_day
from outlook_calendar_agent.mcp_server import build_server
from outlook_calendar_agent.models import EventDraft

from .conftest import FakeGoogleClient, google_event
from .test_mcp_server import _call, _text

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=SGT)  # a Tuesday


@pytest.fixture
def server(grt, monkeypatch):
    monkeypatch.setattr(agent_api, "_now", lambda rt: NOW)
    return build_server(rt_factory=lambda: grt)


def draft_id_of(text: str) -> str:
    return text.split("Draft saved as ")[1].split(".")[0]


# -- phrases ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "rest", "flagged"),
    [
        ("7 nov all day", "7 nov", True),
        ("Full day 7 nov", "7 nov", True),
        ("whole-day 7 nov", "7 nov", True),
        ("all day long tomorrow", "tomorrow", True),
        ("7 nov 全天", "7 nov", True),
        ("7 nov 2pm", "7 nov 2pm", False),
    ],
)
def test_the_all_day_marker_is_taken_out_of_the_phrase(text, rest, flagged) -> None:
    assert strip_all_day(text) == (rest, flagged)


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("7 nov", (date(2026, 11, 7), date(2026, 11, 7))),
        ("tomorrow", (date(2026, 10, 7), date(2026, 10, 7))),
        ("7 nov to 19 nov", (date(2026, 11, 7), date(2026, 11, 19))),
        ("7 Nov - 19 Nov", (date(2026, 11, 7), date(2026, 11, 19))),
        ("7-19 nov", (date(2026, 11, 7), date(2026, 11, 19))),
        ("7 to 19 nov", (date(2026, 11, 7), date(2026, 11, 19))),
        ("30 dec to 2 jan", (date(2026, 12, 30), date(2027, 1, 2))),
        ("7 nov 2pm", None),
        ("next week", None),
        ("", None),
    ],
)
def test_a_day_or_a_span_of_days_is_read(text, days) -> None:
    assert resolve_days(text, now=NOW) == days


def test_a_span_that_ends_before_it_starts_is_asked_about() -> None:
    with pytest.raises(DateAmbiguity, match="ends .* before it starts"):
        resolve_days("19 nov to 7 nov", now=NOW)


# -- the draft -------------------------------------------------------------------------------


def _draft(first: str, after_last: str) -> EventDraft:
    return EventDraft(
        subject="Trip",
        start=datetime.fromisoformat(first).replace(tzinfo=SGT),
        end=datetime.fromisoformat(after_last).replace(tzinfo=SGT),
        all_day=True,
    )


def test_an_all_day_draft_is_sent_as_dates() -> None:
    draft = _draft("2026-11-07T00:00", "2026-11-20T00:00")
    google = draft.to_google_payload(SGT)
    assert (google["start"], google["end"]) == ({"date": "2026-11-07"}, {"date": "2026-11-20"})
    graph = draft.to_graph_payload(SGT)
    assert graph["isAllDay"] is True
    assert graph["start"]["dateTime"] == "2026-11-07T00:00:00"
    assert draft.last_day == date(2026, 11, 19)
    with pytest.raises(ValueError, match="midnight"):
        _draft("2026-11-07T09:00", "2026-11-08T00:00")


# -- creating through the server ---------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"when": "Full day 7 nov"},
        {"when": "7 nov all day"},
        {"when": "7 nov", "all_day": True},
        {"when": "7 nov", "end": "7 nov"},
    ],
)
def test_a_whole_day_is_drafted_without_asking_for_a_time(server, args) -> None:
    out = _text(_call(server, "draft_create_event", subject="SSWA move in", **args))
    assert "Draft saved as" in out, out
    assert "Start            : 2026-11-07 (all day)" in out
    assert "End              : 2026-11-07 (all day)" in out
    assert "Duration         : 1 day, all day" in out
    assert "Conflicts: not checked for an all-day event." in out


@pytest.mark.parametrize(
    "args",
    [
        {"when": "7 nov to 19 nov"},
        {"when": "7-19 nov", "all_day": True},
        {"when": "7 nov", "end": "19 nov"},
        {"when": "7 nov all day", "end": "19 nov"},
    ],
)
def test_several_days_become_one_event_over_those_days(server, args) -> None:
    out = _text(_call(server, "draft_create_event", subject="Overseas trip", **args))
    assert "Draft saved as" in out, out
    assert "Start            : 2026-11-07 (all day)" in out
    assert "End              : 2026-11-19 (all day)" in out
    assert "Duration         : 13 days, all day" in out


def test_a_bare_day_still_asks_for_the_time(server) -> None:
    out = _text(_call(server, "draft_create_event", subject="Dentist", when="7 nov"))
    assert out.startswith("QUESTION:") and "What time on Saturday 07 Nov" in out


def test_an_all_day_event_takes_no_time_and_no_length(server) -> None:
    out = _text(_call(server, "draft_create_event", subject="Trip", when="7 nov 2pm", all_day=True))
    assert out.startswith("QUESTION:") and "has no start time" in out
    out = _text(
        _call(server, "draft_create_event", subject="Trip", when="7 nov all day", end="6pm")
    )
    assert out.startswith("QUESTION:") and "ends on a day, not at a time" in out
    out = _text(
        _call(
            server, "draft_create_event", subject="Trip", when="7 nov all day", duration_minutes=60
        )
    )
    assert out.startswith("ERROR:") and "no length in minutes" in out
    out = _text(_call(server, "draft_create_event", subject="Trip", when="19 nov to 7 nov"))
    assert out.startswith("QUESTION:") and "before it starts" in out


def test_confirming_writes_dates_not_times(server, google: FakeGoogleClient) -> None:
    out = _text(
        _call(server, "draft_create_event", subject="Overseas trip", when="7 nov to 19 nov")
    )
    out = _text(_call(server, "confirm_draft", draft_id=draft_id_of(out), user_reply="yes"))
    assert out.startswith("Created: Overseas trip")
    (call,) = google.write_calls
    assert call.method == "POST"
    assert call.json["start"] == {"date": "2026-11-07"}
    assert call.json["end"] == {"date": "2026-11-20"}  # the day after the last day


def test_a_waiting_all_day_draft_can_be_given_new_days(server) -> None:
    out = _text(_call(server, "draft_create_event", subject="Trip", when="7 nov to 19 nov"))
    first = draft_id_of(out)
    out = _text(_call(server, "draft_update_event", event_id=first, new_end="20 nov"))
    assert f"Replaces draft {first}" in out
    assert "Start            : 2026-11-07 (all day)" in out
    assert "End              : 2026-11-20 (all day)" in out
    second = draft_id_of(out)
    out = _text(_call(server, "draft_update_event", event_id=second, new_when="10 nov"))
    assert "Start            : 2026-11-10 (all day)" in out
    assert "End              : 2026-11-23 (all day)" in out  # the length of 14 days is kept
    out = _text(_call(server, "draft_update_event", event_id=draft_id_of(out), new_when="3pm"))
    assert out.startswith("QUESTION:") and "needs a day rather than a time" in out


# -- moving an existing all-day event ----------------------------------------------------------


def _all_day_event(google: FakeGoogleClient) -> None:
    event = google_event(event_id="allday1", summary="Offsite")
    event["start"], event["end"] = {"date": "2026-10-10"}, {"date": "2026-10-12"}
    google.events_by_id["allday1"] = event
    google.events.append(event)


def test_an_existing_all_day_event_moves_by_day_and_stays_all_day(
    server, google: FakeGoogleClient
) -> None:
    _all_day_event(google)
    out = _text(_call(server, "draft_update_event", event_id="allday1", new_when="14 oct"))
    assert "2026-10-10 (all day)  ->  2026-10-14 (all day)   [CHANGED]" in out
    assert "2026-10-11 (all day)  ->  2026-10-15 (all day)   [CHANGED]" in out
    out = _text(_call(server, "confirm_draft", draft_id=draft_id_of(out), user_reply="yes"))
    assert out.startswith("Updated:")
    (call,) = google.write_calls
    assert call.method == "PATCH"
    assert call.json["start"] == {"date": "2026-10-14"}
    assert call.json["end"] == {"date": "2026-10-16"}


def test_an_existing_all_day_event_is_not_given_a_time(server, google: FakeGoogleClient) -> None:
    _all_day_event(google)
    out = _text(_call(server, "draft_update_event", event_id="allday1", new_when="14 oct 3pm"))
    assert out.startswith("QUESTION:") and "needs a day rather than a time" in out
    out = _text(_call(server, "draft_update_event", event_id="allday1", new_duration_minutes=120))
    assert out.startswith("ERROR:") and "no length in minutes" in out
    assert google.write_calls == []
