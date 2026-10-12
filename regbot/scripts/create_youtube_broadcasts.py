"""Create YouTube live broadcasts and streams from the Wafer schedule.

Talk data (title, abstract, acceptance status) comes from the Wafer talks
API; the schedule ICS feed supplies the start/end times, room names, and
event UIDs, because the API does not expose slot or venue details. Creates
one live broadcast and one dedicated live stream per accepted talk (and per
non-talk schedule entry not excluded in the TOML filter), binds them
together, and adds each broadcast to the playlist that the bot reads
(YOUTUBE_PLAYLIST). Re-running is safe: a state file maps Wafer event UIDs
to the created YouTube resources, so only new or changed events are touched.

Usage (from the project root):

    # Show what would be created, without touching Google:
    uv run python regbot/scripts/create_youtube_broadcasts.py --dry-run

    # Create / update broadcasts and write the stream-keys CSV:
    uv run python regbot/scripts/create_youtube_broadcasts.py

    # Only create the first three talks, to test the pipeline end-to-end
    # (clean up afterwards with --reset):
    uv run python regbot/scripts/create_youtube_broadcasts.py --test-run

    # Delete everything previously created (asks for confirmation):
    uv run python regbot/scripts/create_youtube_broadcasts.py --reset

Environment (dry-run): WAFER_BASE_URL, WAFER_ICS_ENDPOINT,
WAFER_TALKS_ENDPOINT, WAFER_USERNAME, WAFER_PASSWORD.
Environment (full run): the full bot environment, because the OAuth helper is
reused from regbot.google (which reads the Google and, via regbot.helpers,
Discord envars at import time), plus YOUTUBE_PLAYLIST. A valid
.google_client_credentials pickle must exist in the current directory,
otherwise a browser OAuth flow is started (see regbot/google.py).
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import arrow
import httpx
from googleapiclient.errors import HttpError
from ics import Calendar
from jinja2 import Environment, FileSystemLoader

SCRIPT_DIR = Path(__file__).parent
DISPLAY_TIMEZONE = "Africa/Johannesburg"
CREATE_PAUSE_SECONDS = 1

CSV_COLUMNS = [
    "date",
    "start_sast",
    "end_sast",
    "room",
    "title",
    "video_url",
    "stream_key",
    "rtmp_url",
    "studio_url",
]


def get_str_env(env_name: str) -> str:
    """Get the given envar or raise an assertion error."""
    value = os.getenv(env_name)
    assert value, f"The environmental variable {env_name} was not found!"
    return value


def get_youtube_service():
    """Build an authenticated YouTube service with the project's OAuth helper."""
    try:
        import googleapiclient.discovery

        from regbot.google import get_client_credentials
    except (AssertionError, ImportError) as error:
        sys.exit(
            "Could not set up Google credentials. The full bot environment must "
            f"be loaded (see the module docstring). Underlying error: {error}"
        )
    return googleapiclient.discovery.build(
        "youtube", "v3", credentials=get_client_credentials()
    )


TALK_URL_RE = re.compile(r"^/talks/(\d+)-")
WAFER_RATE_LIMIT_PAUSE_SECONDS = 1.5


@dataclass
class ScheduleEvent:
    """A Wafer schedule entry.

    Times, room, and UID come from the ICS feed (the Wafer API does not
    expose slots or venues); talk title, abstract, and acceptance status
    come from the Wafer talks API.
    """

    uid: str
    name: str
    begin: arrow.Arrow
    end: arrow.Arrow
    location: str | None
    description: str | None
    url: str
    is_talk: bool
    accepted: bool


def fetch_wafer_talks() -> dict[int, dict]:
    """Fetch all talks from the Wafer talks API, keyed by talk id."""
    auth = (get_str_env("WAFER_USERNAME"), get_str_env("WAFER_PASSWORD"))
    url: str | None = urljoin(
        get_str_env("WAFER_BASE_URL"), get_str_env("WAFER_TALKS_ENDPOINT")
    )
    talks: dict[int, dict] = {}
    while url:
        response = httpx.get(url, auth=auth, timeout=30)
        response.raise_for_status()
        data = response.json()
        for talk in data["results"]:
            talks[talk["talk_id"]] = talk
        url = data["next"]
        if url:
            # PyConZA's Wafer host is prone to network errors without this.
            time.sleep(WAFER_RATE_LIMIT_PAUSE_SECONDS)
    return talks


def clean_abstract(abstract: str) -> str:
    """Convert a Wafer talk abstract (HTML-ish) to YouTube-friendly plain text."""
    text = re.sub(r"(?i)<br\s*/?>", "\n", abstract)
    text = re.sub(r"(?i)</p>\s*<p[^>]*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\r\n", "\n").strip()


def fetch_schedule_events(talks: dict[int, dict]) -> list[ScheduleEvent]:
    """Fetch the schedule ICS feed and enrich its events with API talk data."""
    base_url = get_str_env("WAFER_BASE_URL")
    ics_url = urljoin(base_url, get_str_env("WAFER_ICS_ENDPOINT"))
    response = httpx.get(ics_url, timeout=30)
    response.raise_for_status()
    ics_events = sorted(Calendar(response.text).events, key=lambda event: event.begin)
    events = []
    for ics_event in ics_events:
        match = TALK_URL_RE.match(ics_event.url or "")
        talk = talks.get(int(match.group(1))) if match else None
        if match:
            # A scheduled talk: the API is authoritative for title/abstract.
            # If it is missing from the API, fall back to the ICS data and
            # assume acceptance (Wafer only schedules accepted talks).
            name = talk["title"] if talk else ics_event.name
            description = clean_abstract(talk["abstract"]) if talk else None
            accepted = talk["status"] == "A" if talk else True
        else:
            name = ics_event.name
            description = None
            accepted = True
        events.append(
            ScheduleEvent(
                uid=ics_event.uid,
                name=name,
                begin=ics_event.begin,
                end=ics_event.end,
                location=ics_event.location,
                description=description,
                url=urljoin(base_url, ics_event.url or ""),
                is_talk=match is not None,
                accepted=accepted,
            )
        )
    return events


def render_snippet(jinja_env: Environment, event: ScheduleEvent) -> dict:
    """Render the title and description templates for an event into an API snippet."""
    context = {
        "event": event,
        "start_sast": event.begin.to(DISPLAY_TIMEZONE),
        "end_sast": event.end.to(DISPLAY_TIMEZONE),
    }
    return {
        "title": jinja_env.get_template("title.j2").render(context).strip(),
        "description": jinja_env.get_template("description.j2").render(context).strip(),
        "scheduledStartTime": event.begin.isoformat(),
        "scheduledEndTime": event.end.isoformat(),
    }


def create_broadcast(youtube, config: dict, snippet: dict) -> dict:
    """Create a live broadcast from the config template and rendered snippet."""
    body = {
        "snippet": snippet,
        "status": config["broadcast"]["status"],
        "contentDetails": config["broadcast"]["contentDetails"],
    }
    request = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails", body=body
    )
    return request.execute()


def create_stream(youtube, config: dict, title: str) -> dict:
    """Create a dedicated live stream for one talk."""
    body = {"snippet": {"title": title}, "cdn": config["stream"]["cdn"]}
    return youtube.liveStreams().insert(part="snippet,cdn", body=body).execute()


def add_to_playlist(youtube, playlist_id: str, broadcast_id: str) -> None:
    """Add a broadcast to the playlist the bot reads."""
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": broadcast_id},
        }
    }
    youtube.playlistItems().insert(part="snippet", body=body).execute()


def needs_update(entry: dict, snippet: dict) -> bool:
    """Check whether the stored broadcast differs from the rendered snippet."""
    return (
        entry["title"] != snippet["title"]
        or entry["description"] != snippet["description"]
        or entry["start"] != snippet["scheduledStartTime"]
        or entry["end"] != snippet["scheduledEndTime"]
    )


def update_broadcast(youtube, entry: dict, snippet: dict) -> None:
    """Update an existing broadcast's snippet (status/contentDetails untouched)."""
    body = {"id": entry["broadcast_id"], "snippet": snippet}
    youtube.liveBroadcasts().update(part="snippet", body=body).execute()
    entry.update(
        title=snippet["title"],
        description=snippet["description"],
        start=snippet["scheduledStartTime"],
        end=snippet["scheduledEndTime"],
    )


def create_for_event(
    youtube, config: dict, playlist_id: str, snippet: dict, event: ScheduleEvent
) -> dict:
    """Create and bind a broadcast and stream for one event, and playlist it."""
    broadcast = create_broadcast(youtube, config, snippet)
    stream = create_stream(youtube, config, snippet["title"])
    youtube.liveBroadcasts().bind(
        part="id", id=broadcast["id"], streamId=stream["id"]
    ).execute()
    add_to_playlist(youtube, playlist_id, broadcast["id"])
    ingestion = stream["cdn"]["ingestionInfo"]
    return {
        "broadcast_id": broadcast["id"],
        "stream_id": stream["id"],
        "title": snippet["title"],
        "description": snippet["description"],
        "start": snippet["scheduledStartTime"],
        "end": snippet["scheduledEndTime"],
        "room": event.location or "",
        "video_url": f"https://youtu.be/{broadcast['id']}",
        "stream_key": ingestion["streamName"],
        "rtmp_url": ingestion["ingestionAddress"],
    }


def load_state(path: Path) -> dict:
    """Load the Wafer event UID to YouTube resource map."""
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict) -> None:
    """Persist the state file."""
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def write_keys_csv(path: Path, state: dict) -> None:
    """(Re)write the stream-keys CSV for the streaming team from the state file."""
    rows = sorted(state.values(), key=lambda entry: entry["start"])
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for entry in rows:
            start = arrow.get(entry["start"]).to(DISPLAY_TIMEZONE)
            end = arrow.get(entry["end"]).to(DISPLAY_TIMEZONE)
            writer.writerow(
                [
                    start.format("YYYY-MM-DD"),
                    start.format("HH:mm"),
                    end.format("HH:mm"),
                    entry["room"],
                    entry["title"],
                    entry["video_url"],
                    entry["stream_key"],
                    entry["rtmp_url"],
                    (
                        f"https://studio.youtube.com/video/{entry['broadcast_id']}"
                        "/livestreaming"
                    ),
                ]
            )


def plan_event(
    event: ScheduleEvent,
    state: dict,
    config: dict,
    jinja_env: Environment,
    include_past: bool,
    now: arrow.Arrow,
) -> tuple[str, dict | None]:
    """Decide what to do with an event. Returns (action, snippet or None)."""
    if event.is_talk:
        # Talks are filtered by their Wafer acceptance status, not by name.
        if not event.accepted:
            return "SKIP (not accepted)", None
    else:
        name = event.name.strip().lower()
        excluded = {
            event_name.lower() for event_name in config["filter"]["exclude_events"]
        }
        if name in excluded:
            return "SKIP (filtered)", None
    if not include_past and event.end < now:
        return "SKIP (past)", None
    snippet = render_snippet(jinja_env, event)
    if event.uid not in state:
        return "CREATE", snippet
    if needs_update(state[event.uid], snippet):
        return "UPDATE", snippet
    return "UNCHANGED", snippet


def plan_actions(
    events: list[ScheduleEvent],
    state: dict,
    config: dict,
    jinja_env: Environment,
    include_past: bool,
    test_run: bool,
) -> list[tuple[ScheduleEvent, str, dict | None]]:
    """Plan the action for every event, narrowing to a test batch if asked."""
    now = arrow.utcnow()
    planned = [
        (event, *plan_event(event, state, config, jinja_env, include_past, now))
        for event in events
    ]
    if not test_run:
        return planned
    actionable = [
        (event, action, snippet)
        for event, action, snippet in planned
        if event.is_talk and action in ("CREATE", "UPDATE")
    ]
    planned = actionable[:3]
    print(f"Test run: limited to the first {len(planned)} actionable talks.")
    return planned


def reset(state: dict, assume_yes: bool) -> int:
    """Delete all broadcasts and streams recorded in the state file."""
    if not state:
        print("State file is empty, nothing to reset.")
        return 0
    if not assume_yes:
        answer = input(
            f"Delete {len(state)} broadcasts and their streams from YouTube? [y/N] "
        )
        if answer.lower() != "y":
            print("Aborted.")
            return 1
    youtube = get_youtube_service()
    failures = 0
    for entry in state.values():
        for kind, request in (
            ("broadcast", youtube.liveBroadcasts().delete(id=entry["broadcast_id"])),
            ("stream", youtube.liveStreams().delete(id=entry["stream_id"])),
        ):
            try:
                request.execute()
                print(f"Deleted {kind} for `{entry['title']}`")
            except HttpError as error:
                print(f"Failed to delete {kind} for `{entry['title']}`: {error}")
                failures += 1
        time.sleep(CREATE_PAUSE_SECONDS)
    return 1 if failures else 0


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without calling the YouTube API.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all broadcasts and streams recorded in the state file.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (with --reset).",
    )
    parser.add_argument(
        "--include-past",
        action="store_true",
        help="Also process events that have already ended.",
    )
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="Only process the first three actionable talks, so a botched run "
        "is easy to clean up with --reset.",
    )
    parser.add_argument(
        "--config", type=Path, default=SCRIPT_DIR / "youtube_template.toml"
    )
    parser.add_argument("--state", type=Path, default=SCRIPT_DIR / ".youtube_state.json")
    parser.add_argument("--keys-csv", type=Path, default=Path("youtube_stream_keys.csv"))
    args = parser.parse_args()

    config = tomllib.loads(args.config.read_text())
    # autoescape is off deliberately: the output is YouTube plain text, not HTML,
    # and HTML-escaping would mangle descriptions (e.g. `&` -> `&amp;`).
    jinja_env = Environment(
        loader=FileSystemLoader(SCRIPT_DIR),
        autoescape=False,  # noqa: S701 (plain text for YouTube, not HTML)
    )
    talks = fetch_wafer_talks()
    events = fetch_schedule_events(talks)
    state = load_state(args.state)

    if args.reset:
        result = reset(state, args.yes)
        if result == 0:
            save_state(args.state, {})
            write_keys_csv(args.keys_csv, {})
        return result

    planned = plan_actions(
        events, state, config, jinja_env, args.include_past, args.test_run
    )

    for event, action, snippet in planned:
        start = event.begin.to(DISPLAY_TIMEZONE).format("ddd D MMM HH:mm")
        title = f" -> {snippet['title']}" if snippet and action != "UNCHANGED" else ""
        print(f"{action:<16} {start}  {event.name}{title}")

    if args.dry_run:
        creates = sum(action == "CREATE" for _, action, _ in planned)
        updates = sum(action == "UPDATE" for _, action, _ in planned)
        print(f"\nDry run: {creates} to create, {updates} to update.")
        return 0

    youtube = get_youtube_service()
    playlist_id = get_str_env("YOUTUBE_PLAYLIST")
    failures = 0
    for event, action, snippet in planned:
        if action not in ("CREATE", "UPDATE"):
            continue
        assert snippet is not None  # CREATE/UPDATE always have a rendered snippet
        try:
            if action == "CREATE":
                state[event.uid] = create_for_event(
                    youtube, config, playlist_id, snippet, event
                )
                print(f"Created `{snippet['title']}`")
                time.sleep(CREATE_PAUSE_SECONDS)
            elif action == "UPDATE":
                update_broadcast(youtube, state[event.uid], snippet)
                print(f"Updated `{snippet['title']}`")
        except HttpError as error:
            print(f"FAILED {event.name}: {error}")
            failures += 1

    save_state(args.state, state)
    write_keys_csv(args.keys_csv, state)
    print(f"Wrote stream keys to {args.keys_csv}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
