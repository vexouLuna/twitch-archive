"""The download and upload pipelines, metadata jobs and the external recorder's callback."""

from __future__ import annotations

from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_common.timeutil import parse_helix_duration
from fastapi import Body, FastAPI
from sqlalchemy import select

from . import (
    jobs,
)
from .admin_shared import AdminContext, AdminError, _actor, _ok, _require
from .vods import upsert_vod


def register(app: FastAPI, ctx: AdminContext) -> None:
    settings = ctx.settings
    service = ctx.service
    auth = ctx.auth
    enqueue = ctx.enqueue
    vod_exists = ctx.vod_exists
    require_vod = ctx.require_vod
    refuse_spliced = ctx.refuse_spliced
    helix_video = ctx.helix_video
    type_payload = ctx.type_payload
    source_payload = ctx.source_payload
    single_part = ctx.single_part

    # ── Download / upload pipelines ───────────────────────────────────────

    @app.post("/admin/download", dependencies=auth)
    async def download(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Download the whole VOD (or use ``path``), split, upload. Optional part range."""
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        for src, dst in (("startPart", "start_part"), ("endPart", "end_part")):
            if body.get(src) not in (None, ""):
                payload[dst] = int(body[src])
        job = await enqueue("download", vod.id, payload)
        await enqueue("emotes", vod.id)
        return _ok("Starting the download…", job)

    @app.post("/admin/hls/download", dependencies=auth)
    async def hls_download(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Full archive pipeline for a VOD (creates the row from Helix if needed)."""
        _require(body, "vodId")
        vod = await vod_exists(body["vodId"])
        if vod is None:
            await upsert_vod(await helix_video(body["vodId"]))
            vod = await require_vod(body["vodId"])
        if await jobs.find_active("archive", vod_id=vod.id):
            raise AdminError(409, f"An archive job for {vod.id} is already running")
        job = await enqueue("archive", vod.id, {"type": "vod", "stream_id": vod.stream_id, "manual": True})
        return _ok(f"Downloading {vod.id} via HLS…", job)

    @app.post("/admin/reupload", dependencies=auth)
    async def reupload(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId", "part")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        part = single_part(payload, body)
        job = await enqueue("reupload", vod.id, payload)
        return _ok(f"Re-uploading {vod.id} part {part}…", job)

    @app.post("/admin/dmca", dependencies=auth)
    async def dmca(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId", "receivedClaims")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        payload["claims"] = body["receivedClaims"]
        job = await enqueue("dmca", vod.id, payload)
        return _ok(f"Muting the DMCA content in {vod.id}…", job)

    @app.post("/admin/part/dmca", dependencies=auth)
    async def part_dmca(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId", "part", "receivedClaims")
        vod = await require_vod(body["vodId"])
        payload = source_payload(vod, body)
        payload["claims"] = body["receivedClaims"]
        part = single_part(payload, body)
        job = await enqueue("part_dmca", vod.id, payload)
        return _ok(f"Trimming the DMCA content from {vod.id} part {part}…", job)

    # ── Metadata ──────────────────────────────────────────────────────────

    @app.post("/admin/logs", dependencies=auth)
    async def logs(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId")
        await require_vod(body["vodId"])
        job = await enqueue("chat", str(body["vodId"]))
        return _ok("Getting the chat logs…", job)

    @app.post("/admin/logs/manual", dependencies=auth)
    async def logs_manual(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId", "path")
        await require_vod(body["vodId"])
        job = await enqueue("logs_manual", str(body["vodId"]), {"path": body["path"]})
        return _ok("Getting the chat logs…", job)

    @app.post("/admin/chapters", dependencies=auth)
    async def chapters(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Chapters from Twitch; ``{"force": true}`` also replaces chapters edited by hand."""
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        await refuse_spliced(vod.id, "Twitch's chapters")  # before asking Twitch; enqueue would refuse after
        video = await helix_video(vod.id)
        payload: dict[str, Any] = {"duration": parse_helix_duration(video.get("duration", ""))}
        if body.get("force") is True:
            payload["force"] = True
        job = await enqueue("chapters", vod.id, payload)
        return _ok(f"Saving Chapters for {vod.id}", job)

    @app.post("/admin/emotes", dependencies=auth)
    async def emotes(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Fill the VOD's missing emote sets; ``{"force": true}`` replaces the saved ones."""
        _require(body, "vodId")
        await require_vod(body["vodId"])
        force = body.get("force") is True
        job = await enqueue("emotes", str(body["vodId"]), {"force": True} if force else None)
        return _ok("Saving emotes (overwriting)…" if force else "Saving emotes…", job)

    async def backfill(kind: str, what: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """One ``kind`` job over every VOD it applies to, or only ``body["vodIds"]``."""
        vod_ids = (body or {}).get("vodIds")
        if vod_ids is not None and not (isinstance(vod_ids, list) and vod_ids):
            raise AdminError(400, "vodIds must be a non-empty list")
        if await jobs.find_active(kind):
            raise AdminError(409, f"A {what} backfill is already running")
        job = await enqueue(kind, None, {"vod_ids": [str(v) for v in vod_ids]} if vod_ids else None)
        return _ok(f"Backfilling {what}…", job)

    @app.post("/admin/emotes/backfill", dependencies=auth)
    async def emotes_backfill(body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """Current global sets onto every emotes row that has none (optionally only ``vodIds``)."""
        return await backfill("global_emotes_backfill", "global emotes", body)

    @app.post("/admin/live-gaps/backfill", dependencies=auth)
    async def live_gaps_backfill(body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """``gaps`` on the live uploads saved without them, from their live jobs (optionally only ``vodIds``)."""
        return await backfill("live_gaps_backfill", "live gaps", body)

    @app.post("/admin/bot-chat", dependencies=auth)
    async def bot_chat(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Read the VOD's chat from doomtp-bot into bot_logs (adds or updates rows only)."""
        _require(body, "vodId")
        if not settings.doomtp_url:
            raise AdminError(500, "ARCHIVE_DOOMTP_URL is not set")
        vod = await require_vod(body["vodId"])
        if await jobs.find_active("bot_chat", vod_id=vod.id):
            raise AdminError(409, f"A bot chat job for {vod.id} is already running")
        job = await enqueue("bot_chat", vod.id)
        return _ok(f"Reading bot chat for {vod.id}…", job)

    @app.post("/admin/bot-chat/backfill", dependencies=auth)
    async def bot_chat_backfill(body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """Bot chat for every VOD without it (optionally only ``vodIds``); merged or split VODs are skipped."""
        if not settings.doomtp_url:
            raise AdminError(500, "ARCHIVE_DOOMTP_URL is not set")
        return await backfill("bot_chat_backfill", "bot chat", body)

    @app.post("/admin/previews", dependencies=auth)
    async def previews(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Seek-bar previews for the VOD's uploads that have none (optionally only ``youtubeIds``), downloaded
        from YouTube."""
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        ids = body.get("youtubeIds")
        if ids is not None and not (isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids)):
            raise AdminError(400, "youtubeIds must be a non-empty list of video ids")
        if await jobs.find_active("previews_fetch", vod_id=vod.id):
            raise AdminError(409, f"A previews job for {vod.id} is already running")
        job = await enqueue("previews_fetch", vod.id, {"youtube_ids": ids} if ids else None)
        return _ok(f"Making previews for {vod.id}…", job)

    @app.post("/admin/previews/backfill", dependencies=auth)
    async def previews_backfill(body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """Seek-bar previews for every VOD with uploads that have none (optionally only ``vodIds``)."""
        return await backfill("previews_backfill", "previews", body)

    @app.post("/admin/youtube/parts", dependencies=auth)
    @app.post("/admin/youtube/chapters", dependencies=auth)
    async def youtube_describe(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId")
        vod = await require_vod(body["vodId"])
        job = await enqueue("describe", vod.id, type_payload(vod, body))
        return _ok(f"Updating YouTube descriptions for {vod.id}", job)

    # ── Live recordings (external recorder callback) ──────────────────────

    @app.post("/v2/live", dependencies=auth)
    async def live(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "streamId", "path")
        stream_id = str(body["streamId"])
        async with get_sessionmaker()() as s:
            vod = (await s.execute(select(Vod).where(Vod.stream_id == stream_id).limit(1))).scalar_one_or_none()
            if vod is None:
                raise AdminError(404, "No VOD has that stream id")
            if body.get("driveId"):
                vod.drive = [*(vod.drive or []), {"id": body["driveId"], "type": "live"}]
                await s.commit()
        # Non-200 tells the recorder to delete its file (legacy contract).
        if not settings.multi_track:
            raise AdminError(404, "multiTrack is disabled")
        # Not the guarded enqueue: a refusal here would make the recorder delete its file.
        job = await service.enqueue(
            "live_file", vod.id, {"type": "live", "stream_id": stream_id, "path": body["path"]}, actor=_actor.get()
        )
        return _ok("Starting the upload to YouTube…", job)
