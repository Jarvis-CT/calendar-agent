"""MCP (Model Context Protocol) server exposing the calendar agent to a supervisor over stdio.

Run with ``outlook-calendar mcp``. Nothing here prints to stdout (the transport uses it); logs go
to stderr. Every mutation still requires ``confirm_draft`` with the user's literal reply ``yes``.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from . import __version__, agent_api, runtime
from .runtime import Runtime

log = logging.getLogger("outlook_calendar_agent.mcp")

INSTRUCTIONS = """Calendar specialist for the user's Google Calendar (times in Asia/Singapore).

Rules for callers:
- Pass date and time phrases exactly as the user said them ("next Tuesday 2pm", "tomorrow",
  "5 Oct to 9 Oct"). This server resolves them deterministically. Do not compute dates yourself.
- A reply starting with "QUESTION:" is a clarification the user must answer; relay it verbatim.
- A reply starting with "ERROR:" explains why something could not be done; relay it.
- draft_* tools never change the calendar. They return a preview and a draft id. Show the
  preview to the user and ask them to reply "yes". Then call confirm_draft(draft_id, reply) with
  the user's literal reply. Only the single word "yes" (any capitalisation, optional trailing
  full stop) applies the draft; anything else discards it.
- Never claim an event was created, moved or deleted unless confirm_draft returned "Created",
  "Updated" or "Deleted".
- When one message from the user leads to several drafts (several events to add, move or
  delete), make every draft first, show all the previews together, then apply them with ONE
  confirm_draft call whose draft_id lists every id separated by commas ("d-1a2b3c, d-4d5e6f").
  That gives the user one approval for the batch. Do not confirm them one at a time.
"""


def build_server(rt_factory=runtime.get_runtime) -> MCPServer:  # type: ignore[no-untyped-def]
    server = MCPServer(
        name="calendar-agent",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
    )
    state: dict[str, Runtime] = {}

    def rt() -> Runtime:
        if "rt" not in state:
            state["rt"] = rt_factory()
        return state["rt"]

    @server.tool(description="Current date, time, weekday and time zone. Call this first.")
    def now() -> str:
        return agent_api.now_text(rt())

    @server.tool(description="List the calendars the user can see, with IDs usable in 'calendar'.")
    def list_calendars() -> str:
        return agent_api.list_calendars_text(rt())

    @server.tool(
        description=(
            "List events. 'when' is the user's phrase: 'today', 'tomorrow', 'next week', "
            "'next Tuesday', '5 Oct to 9 Oct'. Empty = next 7 days. 'calendar' = name or id "
            "(default primary)."
        )
    )
    def list_events(when: str = "", calendar: str = "") -> str:
        return agent_api.list_events_text(rt(), when=when, calendar=calendar)

    @server.tool(
        description=(
            "Find free slots of at least duration_minutes on a day or range ('day' is the user's "
            "phrase), between 'earliest' and 'latest' (e.g. '09:00', '2pm')."
        )
    )
    def find_free_slots(
        day: str,
        duration_minutes: int = 30,
        earliest: str = "09:00",
        latest: str = "18:00",
        calendar: str = "",
    ) -> str:
        return agent_api.find_free_slots_text(
            rt(),
            day,
            duration_minutes=duration_minutes,
            earliest=earliest,
            latest=latest,
            calendar=calendar,
        )

    @server.tool(description="List Google Tasks due in a window ('when' phrase). Read-only.")
    def list_tasks(when: str = "", include_completed: bool = False) -> str:
        return agent_api.list_tasks_text(rt(), when=when, include_completed=include_completed)

    @server.tool(
        description=(
            "Prepare a NEW event draft (does not create it). "
            "subject = a SHORT title only, e.g. 'Lunch', 'Meeting', 'Project review with Alice'; "
            "never put the place, day, time or words like 'another' in it. "
            "location = the place, e.g. 'Shaw Centre'. "
            "when = the user's own start phrase ('Monday 12pm', 'tomorrow 15:30'); never add a "
            "day the user did not say. Pass duration_minutes or end ('6pm') only if the user "
            "gave a length or an end time ('4pm-6pm' -> when '4pm', end '6pm'); otherwise omit "
            "both and a 1 hour default is used and shown. attendees must be e-mail addresses; "
            "send_invitations=true e-mails them. link = a meeting URL the user gave. "
            "reminder_minutes_before: default 1440 (one day), 0 = none, -1 = calendar default. "
            "An event without times ('all day', 'whole day', a trip over several days) is an "
            "all-day event: pass all_day=true with when = the day or the days ('7 Nov', "
            "'7 Nov to 19 Nov'), or when = first day and end = last day; no duration. "
            "Example: 'Monday lunch 12pm shaw centre' -> subject 'Lunch', when 'Monday 12pm', "
            "location 'Shaw Centre'. Returns a preview and a draft id."
        )
    )
    def draft_create_event(
        subject: str,
        when: str,
        duration_minutes: int | None = None,
        end: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        send_invitations: bool = False,
        calendar: str = "",
        link: str = "",
        reminder_minutes_before: int = 1440,
        all_day: bool = False,
    ) -> str:
        return agent_api.draft_create_event_text(
            rt(),
            subject,
            when,
            duration_minutes=duration_minutes,
            end=end,
            location=location,
            attendees=attendees,
            send_invitations=send_invitations,
            calendar=calendar,
            link=link,
            reminder_minutes_before=reminder_minutes_before,
            all_day=all_day,
        )

    @server.tool(
        description=(
            "Prepare a draft that moves/renames/relocates an EXISTING event (does not apply it). "
            "Identify the event with 'find' (subject text) plus 'on' (day phrase), or event_id. "
            "To change a draft that is still waiting (for example 'make it 2 hours'), pass its "
            "draft id (d-xxxxxx) as event_id with only the changed fields. "
            "new_when = new start phrase (duration kept unless new_duration_minutes/new_end). "
            "new_link = a meeting URL to attach ('' removes it). new_reminder_minutes_before "
            "sets the reminder: minutes, 0 = none, -1 = calendar default. notify_attendees=true "
            "e-mails attendees. 'on' may be a day or a range ('next week'). An all-day event "
            "moves by day: new_when = '12 Nov' or '12 Nov to 14 Nov', new_end = its new last "
            "day; it never gains times. Returns a preview and a draft id."
        )
    )
    def draft_update_event(
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
        return agent_api.draft_update_event_text(
            rt(),
            find=find,
            on=on,
            event_id=event_id,
            new_when=new_when,
            new_duration_minutes=new_duration_minutes,
            new_end=new_end,
            new_subject=new_subject,
            new_location=new_location,
            notify_attendees=notify_attendees,
            calendar=calendar,
            new_link=new_link,
            new_reminder_minutes_before=new_reminder_minutes_before,
        )

    @server.tool(
        description=(
            "Prepare a draft that DELETES an existing event (does not delete it). Identify with "
            "'find' + 'on' or event_id. Returns a preview and a draft id."
        )
    )
    def draft_delete_event(
        find: str = "",
        on: str = "",
        event_id: str = "",
        notify_attendees: bool = False,
        calendar: str = "",
    ) -> str:
        return agent_api.draft_delete_event_text(
            rt(),
            find=find,
            on=on,
            event_id=event_id,
            notify_attendees=notify_attendees,
            calendar=calendar,
        )

    @server.tool(description="List drafts waiting for confirmation.")
    def list_drafts() -> str:
        return agent_api.list_drafts_text(rt())

    @server.tool(
        description=(
            "Apply a draft, or a batch of drafts under one approval. draft_id is one draft id, "
            "or several separated by commas ('d-1a2b3c, d-4d5e6f') when the user asked for "
            "several changes in one message; the reply then has one line per draft. Pass the "
            "user's LITERAL reply as user_reply. The drafts are applied only if the reply is "
            "the single word 'yes' (any capitalisation); any other reply discards them. Never "
            "pass 'yes' unless the user actually typed it or tapped a Yes button."
        )
    )
    def confirm_draft(draft_id: str, user_reply: str) -> str:
        return agent_api.confirm_draft_text(rt(), draft_id, user_reply)

    @server.tool(
        description=(
            "Discard pending drafts without applying them. draft_id is one id or several "
            "separated by commas."
        )
    )
    def discard_draft(draft_id: str) -> str:
        return agent_api.discard_draft_text(rt(), draft_id)

    return server


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.WARNING, format="%(levelname)s %(message)s"
    )
    build_server().run(transport="stdio")
