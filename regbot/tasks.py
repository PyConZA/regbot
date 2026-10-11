from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands, tasks
from discord.utils import get

if TYPE_CHECKING:
    from discord.channel import TextChannel

from regbot.helpers import (
    ServerInfo,
    get_int_env,
    log,
    safe_send_message,
    to_discord_title_safe,
)
from regbot.quicket import update_ticket_cache
from regbot.wafer import (
    all_upcoming_events,
    mark_as_announced,
    update_calendar_cache,
    update_speakers_cache,
)
from regbot.youtube import (
    all_upcoming_broadcasts,
    get_all_broadcasts,
    get_broadcast_channels,
    get_youtube_link,
    mark_broadcast_as_announced,
    save_channel_broadcast_map,
)

QUICKET_CACHE_EXPIRE_MINUTES = get_int_env("QUICKET_CACHE_EXPIRE_MINUTES")
WAFER_CACHE_EXPIRE_MINUTES = get_int_env("WAFER_CACHE_EXPIRE_MINUTES")
WAFER_ANNOUNCE_INTERVAL_SECONDS = 30
WAFER_UPCOMING_EVENTS_BOUNDARY_MINUTES = 5

YOUTUBE_CREATE_CHANNELS_MINUTES = 10
YOUTUBE_ANNOUNCE_INTERVAL_SECONDS = 10
YOUTUBE_UPCOMING_BOUNDARY_SECONDS = 20
assert YOUTUBE_ANNOUNCE_INTERVAL_SECONDS < YOUTUBE_UPCOMING_BOUNDARY_SECONDS, (
    "The amount of time between checks (interval) must be sorter than how far into the "
    "future (boundary) broadcasts are announced!"
)


class QuicketSync(commands.Cog):
    """For keeping the quicket database in sync"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync.start()

    @tasks.loop(minutes=QUICKET_CACHE_EXPIRE_MINUTES)
    async def sync(self):
        await log("Refreshing Quicket cache...")
        await update_ticket_cache()
        await log("Quicket cache refreshed.")

    @sync.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()


class WaferSync(commands.Cog):
    """For regularly syncing Wafer data"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync_speakers.start()
        self.sync_events.start()
        self.announcement_loop.start()

    @tasks.loop(minutes=WAFER_CACHE_EXPIRE_MINUTES)
    async def sync_speakers(self):
        await log("Refreshing Wafer speakers cache...")
        await update_speakers_cache()
        await log("Wafer speakers cache refreshed.")

    @sync_speakers.before_loop
    async def before_sync_speakers(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=WAFER_CACHE_EXPIRE_MINUTES)
    async def sync_events(self):
        await log("Refreshing Wafer events cache...")
        await update_calendar_cache()
        await log("Wafer events cache refreshed.")

    @sync_events.before_loop
    async def before_sync_events(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=WAFER_ANNOUNCE_INTERVAL_SECONDS)
    async def announcement_loop(self):
        await self.announce_upcoming_events()

    async def announce_upcoming_events(self):
        """Check for events to announce, then do so and cache to avoid repeating."""
        if not self.bot.guilds:
            return  # The guild is not available yet (e.g. right after connecting).
        server_info = await ServerInfo.get()
        events = await all_upcoming_events(minutes=WAFER_UPCOMING_EVENTS_BOUNDARY_MINUTES)
        for event in events:
            channel_title = to_discord_title_safe(event.name)
            channel = get(server_info.guild.channels, name=channel_title)
            if channel is None:
                await log(
                    f"Skipping announcement for `{event.name}`: "
                    f"no discord channel named `{channel_title}` found."
                )
                continue  # Don't announce or mark as announced.
            await server_info.announcement_channel.send(
                f"The event **{event.name}** is happening in "
                f"{WAFER_UPCOMING_EVENTS_BOUNDARY_MINUTES} minutes!\n"
                f"Please go to {channel.mention} to watch it."
            )
            await mark_as_announced(event)

    @announcement_loop.before_loop
    async def before_announcement_loop(self):
        await self.bot.wait_until_ready()


class YouTubeVideoSync(commands.Cog):
    """Creates, if not already existing, a discord channel for each YouTube channel"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.create_channels.start()
        self.announcement_loop.start()

    @staticmethod
    async def purge_duplicate_titled_channels(
        channels: list[TextChannel],
    ) -> list[TextChannel]:
        """Make sure that only one channel per channel name in the given list remains.
        Not done in any particular order.
        """
        names = set()
        unique_channels = []
        for channel in channels:
            if channel.name in names:
                reason = f"Deleting duplicate talk channel: `{channel}`"
                await log(reason)
                await channel.delete(reason=reason)
            else:
                names.add(channel.name)
                unique_channels.append(channel)
        return unique_channels

    @tasks.loop(minutes=YOUTUBE_CREATE_CHANNELS_MINUTES)
    async def create_channels(self):
        await self.sync_channels()

    async def sync_channels(self):
        """Create discord channels that mirror YouTube channels if they don't yet exist."""
        if not self.bot.guilds:
            return  # The guild is not available yet (e.g. right after connecting).
        broadcasts = get_all_broadcasts()
        await log("Found broadcasts, iterating through them...")
        server_info = await ServerInfo.get()
        category = server_info.youtube_category
        existing_channels = await self.purge_duplicate_titled_channels(category.channels)
        existing_channels = {channel.name: channel for channel in existing_channels}

        current_names = set()
        channel_broadcast_map = {}
        for i, broadcast in enumerate(broadcasts):
            if broadcast["title"] in current_names:
                continue
            position = 100 + i if broadcast["over_hour_old"] else i
            if broadcast["title"] in existing_channels:
                await log(
                    f"Found existing channel for, so updating it `{broadcast['title']}`"
                )
                channel = existing_channels[broadcast["title"]]
                await channel.edit(
                    position=position,
                    topic=broadcast["description"],
                )
            else:
                await log(f"Creating channel for talk: `{broadcast['title']}`")
                channel = await category.create_text_channel(
                    name=broadcast["title"],
                    reason="Talk YouTube channel",
                    position=position,
                    topic=broadcast["description"],
                )
                message = await channel.send(
                    f"__**Talk title**__: {broadcast['original_title']}"
                )
                await message.pin(reason="Talk title")
                message = await channel.send(
                    f"__**Talk link**__: {get_youtube_link(broadcast['id'])}"
                )
                await message.pin(reason="Talk link")
                messages = await safe_send_message(
                    channel,
                    f"__**Talk description**__:\n{broadcast['original_description']}",
                )
                for message in messages:
                    await message.pin(reason="Talk description")

            current_names.add(broadcast["title"])
            channel_broadcast_map[channel] = broadcast
        await log("Completed making broadcast channels.")
        save_channel_broadcast_map(channel_broadcast_map)

        existing_names = set(existing_channels.keys())
        to_delete = existing_names.difference(current_names)
        for name in to_delete:
            reason = f"Deleting {name} as there is no broadcast tied to it."
            await log(reason)
            await existing_channels[name].delete(reason=reason)

    @create_channels.before_loop
    async def before_create_channels(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=YOUTUBE_ANNOUNCE_INTERVAL_SECONDS)
    async def announcement_loop(self):
        await self.announce_starting_broadcasts()

    async def announce_starting_broadcasts(self):
        """Check for broadcasts to announce, then do so and cache to avoid repeating."""
        if not self.bot.guilds:
            return  # The guild is not available yet (e.g. right after connecting).
        server_info = await ServerInfo.get()
        channels = await all_upcoming_broadcasts(
            seconds=YOUTUBE_UPCOMING_BOUNDARY_SECONDS
        )
        channel_map = get_broadcast_channels()
        for channel in channels:
            broadcast = channel_map[channel]
            await channel.send(
                f"{server_info.attendee.mention} This talk is starting now!\n"
                f"**Talk Link**: {get_youtube_link(broadcast['id'])}"
            )
            await channel.send(
                "Remember that you can ask a question with the `!question` command like "
                "so:\n`!question your question text here`."
                "\n But remember to keep it short, as it must be no longer than 200 "
                "characters after we add your name to the question."
            )
            mark_broadcast_as_announced(channel)

    @announcement_loop.before_loop
    async def before_announcement_loop(self):
        await self.bot.wait_until_ready()
