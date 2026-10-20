# calendar agent (Google Calendar first, Outlook optional)

A small, local, terminal-first agent for your calendar on Ubuntu. It talks to the
**Google Calendar API** directly (or to Microsoft Graph for Outlook, if you choose that
provider), signs in with **OAuth 2.0 as you**, and treats every calendar write as a *proposal*
that must be confirmed by typing the single word `yes` (any capitalisation).

All times are displayed in **Asia/Singapore**. Reads are time-bounded queries of the primary
calendar. No desktop apps, no browser automation, no web server, no database.

## Status

| Milestone | State |
|-----------|-------|
| 1. Read-only: login, whoami, bounded event listing, draft preview model | done |
| 2. Controlled writes: draft-create/create, draft-update/update, draft-delete/delete, conflict checks, audit log | done |
| 2b. Google Calendar provider (default); Microsoft Graph kept as `CALENDAR_PROVIDER=microsoft` | done |
| 3. Natural-language chat loop (LLM) on top of the same drafts/confirmation gate | not started |

## 1. Google Cloud setup (one-time, manual)

1. https://console.cloud.google.com → create a project (e.g. `calendar-agent`).
2. **APIs & Services → Library** → search *Google Calendar API* → **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - User type **External**; app name and your e-mail as support/developer contact.
   - **Audience → Test users** → add your own Gmail address.
   - Leave the app in *Testing*. Google then expires refresh tokens after **7 days**, so you
     will re-run `login` weekly. Publishing the app removes that limit.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   application type **Desktop app**. Copy the **Client ID** and **Client secret**.

Why a client secret? Google's device-code flow does not allow Calendar scopes, so the agent
uses the installed-app authorization-code flow, and Google requires the Desktop client's secret
for the token exchange. Google documents this secret as *not* confidential for installed apps.
It lives in `.env` (gitignored) and is never printed or logged.

## 2. Local setup

```bash
cd ~/agents/outlook-calendar-agent
uv sync
cp .env.example .env
$EDITOR .env            # set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
```

`.env`:

```dotenv
CALENDAR_PROVIDER=google
GOOGLE_CLIENT_ID=xxxxxxxx-xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_SCOPES=calendar.events          # or calendar.events.readonly for a read-only agent
```

State (token file, drafts, audit log) lives outside the repo in
`$XDG_STATE_HOME/outlook-calendar-agent/` (default `~/.local/state/outlook-calendar-agent/`),
directory mode `700`, files mode `600`. Override with `CALENDAR_AGENT_STATE_DIR`.

## 3. Commands

### Sign in and read

```bash
uv run outlook-calendar login            # opens a browser on this machine (loopback redirect)
uv run outlook-calendar login --manual   # over SSH: open the URL anywhere, paste the redirected URL back
uv run outlook-calendar whoami
uv run outlook-calendar events --days 7  # today + next 6 days, Asia/Singapore
uv run outlook-calendar events --from 2026-10-05T00:00:00 --to 2026-10-12T00:00:00
uv run outlook-calendar logout           # revokes the token (best effort) and deletes the token file
uv run outlook-calendar calendars        # list every calendar you can see, with IDs
uv run outlook-calendar events --days 7 --calendar "Work"   # any calendar by name or ID
uv run outlook-calendar tasks --days 7   # Google Tasks due in the window (read-only)
```

Google Tasks are not calendar events: the Calendar API never returns them. The `tasks` command
reads them through the Tasks API when `tasks.readonly` is in `GOOGLE_SCOPES` (default) and the
**Google Tasks API is enabled** in your Cloud project (APIs & Services → Library). Tasks only have
a due date, so they are listed by date.

Every command that targets events (`events`, `draft-create`, `create`, `draft-update`,
`draft-delete`) accepts `--calendar <name or ID>`; the default is your primary calendar. Write
commands refuse read-only calendars up front. A draft remembers its calendar, so `update`/`delete`
act on the right one, and the audit log records it. Conflict checks scan the target calendar plus
every calendar you can write to, so a new event on "Work" still reports a clash with your primary. The agent always requests the
read-only `calendar.calendarlist.readonly` scope in addition to `GOOGLE_SCOPES` so it can resolve
calendar names.

Example output:

```text
Signed in as: Chia Geng <user@gmail.com>

Events 2026-10-05 00:00 to 2026-10-12 00:00 — Asia/Singapore
2026-10-05
  10:00–10:30  Team stand-up
  14:00–15:00  Project review | Teams | organizer@example.com
```

Date/time inputs accept `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM` (interpreted as Asia/Singapore),
an explicit offset (`...+08:00`, `...Z`), and the words `today` / `tomorrow`.

`login --manual` over SSH: after you approve in the browser, Google redirects to a
`http://127.0.0.1:8765/?...` address that your browser cannot reach. That is expected. Copy the
full URL from the address bar and paste it into the terminal prompt.

### Create (two steps)

```bash
# Step 1: preview + conflict check + save a draft. No write happens.
uv run outlook-calendar draft-create \
  --subject "Project review" --start 2026-10-07T14:00 --duration 45 \
  --location "Room 4" --attendee "Alice Tan <alice@example.com>" --send-invitations
# → prints the full preview and "Draft saved as d-1a2b3c"

# Step 2: re-print the preview and ask "Create this event? Type yes to continue (anything else cancels):"
uv run outlook-calendar create --draft d-1a2b3c
```

Rules enforced by the tool:
- Attendees are only e-mailed when you pass `--send-invitations` (Google `sendUpdates=all`).
  Without it they are added to the event silently, and the preview says so.
  On the Microsoft provider, which cannot suppress e-mails, attendees are refused without the flag.
- `--end` or `--duration` (minutes, default 30). Meet/Teams links and recurrence are never set.
- `create` also accepts the same inline flags as `draft-create` for a one-shot flow; it still prompts.

### Update / move (two steps)

```bash
uv run outlook-calendar draft-update --find "project review" --on tomorrow --start 2026-10-07T16:00
#   If several events match, the tool lists them with IDs and exits; re-run with --event-id.
#   Moving --start without --end keeps the original duration.
uv run outlook-calendar update --draft d-9f8e7d      # prompts: Update this event? Type yes to continue...
```

- `--notify-attendees` e-mails existing attendees about the change; otherwise it is silent.
- Only single events organised by you can be updated. Recurring series and their instances, and
  events you do not organise, are refused with a clear message.
- The update is sent with `If-Match: <etag>`; if the event changed since the draft, Google
  returns 412 and the tool refuses. The event is also re-read and compared before prompting.

### Delete (two steps)

```bash
uv run outlook-calendar draft-delete --event-id abc123def456 [--notify-attendees]
uv run outlook-calendar delete --draft d-5c4b3a
# prints the preview, then a PERMANENT DELETE block with Event ID, Subject, Start and End (Asia/Singapore)
# and asks: Delete this event permanently? Type yes to continue (anything else cancels):
```

Recurring series and their instances are never deleted by this tool.

### Drafts and audit

```bash
uv run outlook-calendar drafts                 # list saved drafts
uv run outlook-calendar discard --draft d-...  # delete a draft locally (no API call)
cat ~/.local/state/outlook-calendar-agent/audit.jsonl
```

Each audit line records `action` (create/update/delete), `stage`
(proposed/confirmed/rejected/succeeded/failed), draft ID, event ID, subject, start and end.
Tokens and credentials are never written; the logger rejects any key that looks like one.

## 3b. Using the agent from a supervisor (MCP)

`uv run outlook-calendar mcp` runs an [MCP](https://modelcontextprotocol.io) server over stdio so
another agent (the Telegram supervisor in `supervisor-agent`) can call the calendar as tools:
`now`, `list_calendars`, `list_events`, `find_free_slots`, `list_tasks`, `draft_create_event`,
`draft_update_event`, `draft_delete_event`, `list_drafts`, `confirm_draft`, `discard_draft`.

- Date and time arguments are the user's own phrases ("next Tuesday 2pm", "5 Oct to 9 Oct") and
  are resolved deterministically by `dates.py`; vague phrases come back as `QUESTION: ...`.
- `draft_*` tools never write. They return the preview and a draft id.
- An event without times is an all-day event: "full day 7 Nov", "7 Nov all day", a span such as
  "7 Nov to 19 Nov" (or "7-19 Nov"), a first day with a last day as `end`, or `all_day=true`.
  It is stored as dates, not times; conflicts are not checked for it; and `draft_update_event`
  moves it by day ("12 Nov") without ever giving it a time. A bare day ("7 Nov") still asks
  for the time, because it is more likely an appointment than a whole day.
- `confirm_draft(draft_id, user_reply)` applies the draft only when `user_reply` is the single word `yes`
  and discards it otherwise, so the confirmation gate is enforced in this repo, not in any LLM.
- Errors come back as `ERROR: ...` text so the calling model can relay them.

## 4. Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests use recording fake API clients for both providers. They prove, among other things, that
`create`, `update` and `delete` make **zero** POST/PATCH/DELETE calls for any answer other than
the single word `yes` (including `y`, `ok`, `yes please`, empty input and EOF), and that the Google sign-in
uses PKCE, checks `state`, stores the token file with mode 600 and never prints a token.

## 5. Security model

- The agent acts only as the account you sign in with. OAuth scopes are the minimum needed:
  `calendar.events` (or `calendar.events.readonly`), `calendar.calendarlist.readonly` to list
  calendars, plus `openid email` for `whoami`.
- Least privilege via `GOOGLE_SCOPES`; a read-only configuration makes every write command exit
  with a configuration error before any network call.
- Token file, drafts and audit log are owner-only files outside the repository. `.gitignore`
  excludes `.env`, token files, JSONL logs, drafts and calendar exports.
- Tokens are never printed or logged. Typer's pretty tracebacks (which can dump local variables)
  are disabled. The loopback HTTP handler does not log request lines (they carry the code).
- Every mutation prints a structured preview and requires the single word `yes` (any capitalisation).
- Conflict checks are bounded `events.list` reads over the proposed window.
- Exit codes: 1 generic, 2 config, 3 auth, 4 permission, 5 API, 6 network, 8 ambiguous event,
  9 unsupported operation, 10 not confirmed, 11 stale draft.

## 6. Microsoft 365 / Outlook.com (optional provider)

Set `CALENDAR_PROVIDER=microsoft`, `MS_CLIENT_ID`, `MS_TENANT_ID` (and optionally `MS_SCOPES`)
in `.env`. Entra app registration: *Allow public client flows* = Yes, delegated permissions
`User.Read`, `Calendars.Read`, `Calendars.ReadWrite`, no client secret. Sign-in uses the
device-code flow. Note that a personal Microsoft account needs its own Entra tenant (an Azure
free account creates one) before an app can be registered.

## 7. Intentionally deferred

- Natural-language chat / LLM interpretation (the drafts + `yes` gate are the groundwork).
- Google Meet / Teams links (`conferenceData` / `isOnlineMeeting` are never set).
- Recurrence: creating recurring events, or updating/deleting any series or instance.
- Changing the organizer; updating events you did not organise.
- Attendee lookup by name (People / Contacts). Attendees must be e-mail addresses.
- Secondary calendars; everything targets the primary calendar.
- Encrypted token storage, automatic retry on 429, and a `--yes` bypass flag (deliberately not
  provided).
