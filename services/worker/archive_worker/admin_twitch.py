"""Twitch: categories, the channel's videos and importing them, and the linked viewer account."""

from __future__ import annotations

import secrets
import time
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_common.serialize import box_art_template
from archive_common.timeutil import format_hhmmss, parse_helix_duration
from fastapi import Body, FastAPI, Request
from sqlalchemy import select
from vex_platform.actor import Actor

from . import (
    jobs,
    twitch_viewer,
)
from .admin_shared import AdminContext, AdminError, _actor, _ok
from .vods import upsert_vod


def register(app: FastAPI, ctx: AdminContext) -> None:
    deps = ctx.deps
    settings = ctx.settings
    helix = ctx.helix
    auth = ctx.auth
    enqueue = ctx.enqueue
    vod_exists = ctx.vod_exists
    require_vod = ctx.require_vod
    require_helix = ctx.require_helix
    viewer_check = ctx.viewer_check

    @app.get("/admin/twitch/games", dependencies=auth)
    async def search_games(query: str = "") -> list[dict[str, Any]]:
        """Twitch categories matching ``query`` (for picking a chapter's game)."""
        if not query.strip():
            raise AdminError(400, "Missing parameter: query")
        require_helix()
        return [
            {"gameId": c.get("id"), "name": c.get("name"), "imageTemplate": box_art_template(c.get("box_art_url"))}
            for c in await helix.search_categories(query.strip())
        ]

    async def channel_videos() -> list[dict[str, Any]]:
        """The channel's past broadcasts still on Twitch, newest first."""
        require_helix()
        return await helix.all_videos(str(settings.twitch_id))

    @app.get("/admin/twitch/videos", dependencies=auth)
    async def twitch_videos() -> dict[str, Any]:
        """The channel's past broadcasts still on Twitch, each with what the archive has of it: a row
        (``inArchive``), uploads (``uploaded``) and a queued, running or paused archive job (``archiveJob``)."""
        videos = await channel_videos()
        ids = [str(v["id"]) for v in videos]
        async with get_sessionmaker()() as s:
            rows = {v.id: v for v in (await s.execute(select(Vod).where(Vod.id.in_(ids)))).scalars()}
        out = []
        for video in videos:
            row = rows.get(str(video["id"]))
            job = await jobs.find_active("archive", vod_id=str(video["id"]))
            out.append(
                {
                    "id": str(video["id"]),
                    "title": video.get("title"),
                    "createdAt": video.get("created_at"),
                    "duration": format_hhmmss(parse_helix_duration(video.get("duration", ""))),
                    "thumbnailUrl": video.get("thumbnail_url") or None,
                    "inArchive": row is not None,
                    "uploaded": bool(row is not None and row.youtube),
                    "archiveJob": None if job is None else {"id": job.id, "state": job.state, "step": job.step},
                }
            )
        return {"data": out}

    @app.post("/admin/twitch/import", dependencies=auth)
    async def twitch_import(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Queue an archive job for each of ``vodIds`` (creating its row from Twitch when missing), paused
        before its first step unless ``paused`` is false. A VOD no longer on Twitch, or with an archive
        job already queued, running or paused, is skipped."""
        vod_ids = body.get("vodIds")
        if not (isinstance(vod_ids, list) and vod_ids and all(isinstance(v, str) and v for v in vod_ids)):
            raise AdminError(400, "vodIds must be a list of VOD ids")
        paused = body.get("paused", True) is not False
        videos = {str(v["id"]): v for v in await channel_videos()}
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for vod_id in dict.fromkeys(vod_ids):
            video = videos.get(vod_id)
            if video is None:
                skipped.append({"vodId": vod_id, "reason": "Not on Twitch"})
                continue
            active = await jobs.find_active("archive", vod_id=vod_id)
            if active is not None:
                skipped.append({"vodId": vod_id, "reason": f"Archive job {active.id} is already {active.state}"})
                continue
            if await vod_exists(vod_id) is None:
                await upsert_vod(video)
            vod = await require_vod(vod_id)
            payload = {"type": "vod", "stream_id": vod.stream_id, "manual": True}
            job = await enqueue("archive", vod.id, payload, paused=paused)
            added.append({"vodId": vod_id, "jobId": job.id})
        state = "paused" if paused else "queued"
        return _ok(f"{len(added)} archive job(s) {state}, {len(skipped)} skipped", added=added, skipped=skipped)

    # ── Twitch viewer account (live playback tokens without stitched ads) ─

    # Device flows in progress, by an id the dashboard polls with; the device code never leaves the worker.
    linking: dict[str, tuple[float, str, Actor]] = {}  # id -> (monotonic expiry, device code, who started it)

    @app.get("/admin/twitch/viewer", dependencies=auth)
    async def twitch_viewer_status() -> dict[str, Any] | None:
        """The linked account and whether Twitch still accepts its token (checked now)."""
        return await viewer_check(fresh=True)

    @app.post("/admin/twitch/viewer/device", dependencies=auth)
    async def twitch_viewer_device(request: Request) -> dict[str, Any]:
        """Start linking: the code to enter at ``verificationUri`` (twitch.tv/activate), signed in as the
        account to link. Poll ``GET /admin/twitch/viewer/device/{flowId}`` every ``interval`` seconds."""
        try:
            flow = await twitch_viewer.start_device(settings)
        except twitch_viewer.ViewerTokenError as exc:
            # Not a 5xx: Cloudflare puts its own page in place of a 502, and the dashboard would lose why.
            raise AdminError(400, str(exc)) from exc
        now = time.monotonic()
        for key in [k for k, (until, _, _) in linking.items() if until < now]:
            del linking[key]
        flow_id = secrets.token_urlsafe(16)
        linking[flow_id] = (now + int(flow["expires_in"]), flow["device_code"], _actor.get())
        request.state.audit_target = twitch_viewer.AUDIT_TARGET
        return {
            "flowId": flow_id,
            "userCode": flow["user_code"],
            "verificationUri": flow["verification_uri"],
            "expiresIn": int(flow["expires_in"]),
            "interval": int(flow.get("interval") or 5),
        }

    @app.get("/admin/twitch/viewer/device/{flow_id}", dependencies=auth)
    async def twitch_viewer_device_poll(flow_id: str) -> dict[str, Any]:
        """``state``: ``pending`` until the code is entered, then ``linked`` (with ``status``), or ``failed``
        (with ``error``: expired, refused, or a token Twitch won't validate)."""
        found = linking.get(flow_id)
        if found is None or found[0] < time.monotonic():
            linking.pop(flow_id, None)
            return {"state": "failed", "error": "This link expired; start again"}
        _, device_code, actor = found
        before = twitch_viewer.token_summary(await twitch_viewer.load_token())
        try:
            token = await twitch_viewer.poll_device(settings, device_code)
        except twitch_viewer.ViewerTokenError as exc:
            linking.pop(flow_id, None)
            await twitch_viewer.audit_link(actor, before, None, error=str(exc))
            return {"state": "failed", "error": str(exc)}
        if token is None:
            return {"state": "pending"}
        linking.pop(flow_id, None)
        deps.viewer.forget()
        await twitch_viewer.audit_link(actor, before, twitch_viewer.token_summary(token))
        return {"state": "linked", "status": await viewer_check(fresh=True)}

    @app.put("/admin/twitch/viewer", dependencies=auth)
    async def twitch_viewer_paste(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any] | None:
        """Link by pasting a token (``{"token": ...}``): the ``auth-token`` cookie of twitch.tv signed in as
        the account, when the device flow's token doesn't get an ad-free stream. Audited without it."""
        raw = str(body.get("token") or "").strip().removeprefix("OAuth ").strip()
        if not raw:
            raise AdminError(400, "token is required")
        before = twitch_viewer.token_summary(await twitch_viewer.load_token())
        try:
            token = await twitch_viewer.store(raw, method="paste")
        except twitch_viewer.ViewerTokenError as exc:
            raise AdminError(400, str(exc)) from exc
        deps.viewer.forget()
        request.state.audit_target = twitch_viewer.AUDIT_TARGET
        request.state.audit_detail = {"before": before, "after": twitch_viewer.token_summary(token)}
        return await viewer_check(fresh=True)

    @app.delete("/admin/twitch/viewer", dependencies=auth)
    async def twitch_viewer_unlink(request: Request) -> dict[str, Any]:
        """Forget the linked account: live playback tokens are asked for anonymously again. The token is
        not revoked (a pasted one is the account's website sign-in); sign out on Twitch to end it."""
        before = twitch_viewer.token_summary(await twitch_viewer.load_token())
        await twitch_viewer.clear_token()
        deps.viewer.forget()
        request.state.audit_target = twitch_viewer.AUDIT_TARGET
        request.state.audit_detail = {"before": before, "after": None}
        return _ok("Twitch account unlinked")
