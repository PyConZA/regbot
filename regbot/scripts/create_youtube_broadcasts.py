"""Create YouTube live broadcasts and streams from the Wafer schedule.

Creates one live broadcast and one dedicated live stream per talk event in the
Wafer schedule ICS feed, binds them together, and adds each broadcast to the
playlist that the bot reads (YOUTUBE_PLAYLIST). Re-running is safe: a state
file maps Wafer event UIDs to the created YouTube resources, so only new or
changed events are touched.

Usage (from the project root):

    # Show what would be created, without touching Google:
    uv run python regbot/scripts/create_youtube_broadcasts.py --dry-run

    # Create / update broadcasts and write the stream-keys CSV:
    uv run python regbot/scripts/create_youtube_broadcasts.py

    # Delete everything previously created (asks for confirmation):
    uv run python regbot/scripts/create_youtube_broadcasts.py --reset

Environment (dry-run): WAFER_BASE_URL, WAFER_ICS_ENDPOINT.
Environment (full run): the full bot environment, because the OAuth helper is
reused from regbot.google (which reads the Google and, via regbot.helpers,
Discord envars at import time), plus YOUTUBE_PLAYLIST. A valid
.google_client_credentials pickle must exist in the current directory,
otherwise a browser OAuth flow is started (see regbot/google.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urljoin

import arrow
import httpx
from googleapiclient.errors import HttpError
from ics import Calendar, Event
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


def fetch_schedule_events() -> list[Event]:
    """Fetch the Wafer schedule ICS feed and return all events, sorted by start."""
    ics_url = urljoin(get_str_env("WAFER_BASE_URL"), get_str_env("WAFER_ICS_ENDPOINT"))
    response = httpx.get(ics_url, timeout=30)
    response.raise_for_status()
    return sorted(Calendar(response.text).events, key=lambda event: event.begin)


def render_snippet(jinja_env: Environment, event: Event) -> dict:
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
    youtube, config: dict, playlist_id: str, snippet: dict, event: Event
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
    event: Event,
    state: dict,
    config: dict,
    jinja_env: Environment,
    include_past: bool,
    now: arrow.Arrow,
) -> tuple[str, dict | None]:
    """Decide what to do with an event. Returns (action, snippet or None)."""
    name = event.name.lower()
    if any(keyword in name for keyword in config["filter"]["exclude_keywords"]):
        return "SKIP (filtered)", None
    if not include_past and event.end < now:
        return "SKIP (past)", None
    snippet = render_snippet(jinja_env, event)
    if event.uid not in state:
        return "CREATE", snippet
    if needs_update(state[event.uid], snippet):
        return "UPDATE", snippet
    return "UNCHANGED", snippet


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
    events = fetch_schedule_events()
    state = load_state(args.state)

    if args.reset:
        result = reset(state, args.yes)
        if result == 0:
            save_state(args.state, {})
            write_keys_csv(args.keys_csv, {})
        return result

    now = arrow.utcnow()
    planned = [
        (event, *plan_event(event, state, config, jinja_env, args.include_past, now))
        for event in events
    ]

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
