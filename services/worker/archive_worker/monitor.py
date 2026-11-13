"""Twitch watcher (replaces the legacy src/check.js).

Every ``monitor_interval_seconds``: look up the channel's live stream, keep the
``streams`` row current, and enqueue one ``live`` job (live_record) and one
``archive`` job (vod_download) per stream. With live_record on and multi_track off the VOD copy
is not uploaded, so no archive job is queued: the live job saves the chapters, chat and emotes.
When a stream ends, enqueue one ``bot_chat`` job (doomtp_url) for its VOD: the bot records the
chat live, so it is read once, at the end.
With ``auto_merge`` a new VOD is merged with the one before when the break between them is short
(``synthetic.auto_merge``), when it is first seen: before any of it is uploaded.
Each round also recomposes the synthetic VODs whose sources changed (``synthetic.recompose_stale``),
with or without Twitch credentials.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from archive_common.config import Settings
from archive_common.timeutil import parse_helix_duration, parse_ts
from archive_common.twitch.helix import Helix

from . import jobs, synthetic
from .vods import live_stream_ids, set_live_stream, upsert_vod, vod_id_for_stream

log = logging.getLogger(__name__)

VOD_WAIT_SECONDS = 3600  # how long an ended stream stays live while Twitch has not listed its VOD


class Monitor:
    def __init__(self, helix: Helix, service: jobs.JobService, settings: Settings) -> None:
        self.helix = helix
        self.service = service
        self.settings = settings  # the worker's one Settings object, which dashboard overrides change
        self._waiting_since: dict[str, float] = {}  # ended stream id -> when its VOD was first missing

    async def run_forever(self) -> None:
        watching = self.helix.configured
        if not watching:
            log.warning("Twitch client id/secret not set; monitor disabled (synthetic VODs are still recomposed)")
        while True:
            if watching:
                try:
                    await self.tick()
                except Exception:
                    log.exception("monitor tick failed")
            try:
                await synthetic.refresh_auto()
            except Exception:
                log.exception("refreshing auto playthroughs failed")
            try:
                await synthetic.recompose_stale()
            except Exception:
                log.exception("recomposing synthetic VODs failed")
            await asyncio.sleep(self.settings.monitor_interval_seconds)

    async def tick(self) -> None:
        s = self.settings
        stream = await self.helix.get_stream(s.twitch_id)
        # Before they are marked offline, so a failed enqueue is retried on the next tick. A stream whose
        # VOD is not listed yet stays live (and is retried) for up to VOD_WAIT_SECONDS.
        waiting: set[str] = set()
        for ended in await live_stream_ids() if s.doomtp_url else ():
            if stream and ended == str(stream["id"]):
                continue
            if await self.stream_ended(ended):
                self._waiting_since.pop(ended, None)
                continue
            first = self._waiting_since.setdefault(ended, time.monotonic())
            if time.monotonic() - first < VOD_WAIT_SECONDS:
                waiting.add(ended)
            else:
                log.warning("stream %s has no VOD after %ss; giving up on its bot chat", ended, VOD_WAIT_SECONDS)
                self._waiting_since.pop(ended, None)
        if not stream:
            await set_live_stream(None, keep=waiting)
            return
        stream_id = str(stream["id"])
        await set_live_stream(stream_id, parse_ts(stream.get("started_at")), keep=waiting)

        if s.live_record and not await jobs.exists_any("live", stream_id=stream_id):
            log.info("stream %s is live; starting live recording", stream_id)
            payload = {"type": "live", "stream_id": stream_id, "login": s.twitch_username}
            await self.service.enqueue("live", None, payload)

        if not s.vod_download:
            return
        live_only = s.live_record and not s.multi_track  # the VOD copy would not be uploaded
        if live_only:
            if await vod_id_for_stream(stream_id):
                return
        elif await jobs.exists_any("archive", stream_id=stream_id):
            return
        video = await self.helix.video_for_stream(s.twitch_id, stream_id)
        if video is None:
            log.info("stream %s has no VOD yet", stream_id)
            return
        await upsert_vod(video)
        if s.auto_merge:
            try:
                if merged := await synthetic.auto_merge(str(video["id"]), s.auto_merge_gap_minutes):
                    log.info("vod %s merged into %s", video["id"], merged)
            except Exception:
                log.exception("auto merge of vod %s failed", video["id"])
        if live_only:
            log.info("stream %s -> vod %s; the live job archives it", stream_id, video["id"])
            return
        log.info("stream %s -> vod %s; starting archive job", stream_id, video["id"])
        await self.service.enqueue("archive", video["id"], {"type": "vod", "stream_id": stream_id})

    async def stream_ended(self, stream_id: str) -> bool:
        """True when nothing is left to do for the ended stream; False when its VOD is not listed yet."""
        s = self.settings
        if await jobs.exists_any("bot_chat", stream_id=stream_id):
            return True
        video = await self.helix.video_for_stream(s.twitch_id, stream_id)
        if video is None:
            log.info("stream %s ended and has no VOD yet; will retry", stream_id)
            return False
        await upsert_vod(video)
        payload: dict[str, Any] = {"stream_id": stream_id}
        if duration := parse_helix_duration(video.get("duration", "")):
            payload["duration"] = duration  # final now; the vods row may still hold the live one
        log.info("stream %s -> vod %s ended; starting bot chat job", stream_id, video["id"])
        await self.service.enqueue("bot_chat", video["id"], payload)
        return True
