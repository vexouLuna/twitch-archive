"""VOD rows: creating, deleting, the duration, the dashboard's read of one VOD and its merge candidates.

Editing them is /api/v2's (api_v2_routes.py)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Log, Vod
from archive_common.serialize import vod_json
from archive_common.timeutil import format_hhmmss, hhmmss_to_seconds, parse_helix_duration
from fastapi import Body, FastAPI
from sqlalchemy import delete, select, update

from . import (
    synthetic,
)
from .admin_shared import RECENT_JOBS, AdminContext, AdminError, _job_json, _ok, _require
from .events import iso_utc
from .job_rows import ALL_JOBS
from .vods import notify_rows_moved, upsert_vod


def register(app: FastAPI, ctx: AdminContext) -> None:
    settings = ctx.settings
    auth = ctx.auth
    enqueue = ctx.enqueue
    vod_exists = ctx.vod_exists
    require_vod = ctx.require_vod
    refuse_spliced = ctx.refuse_spliced
    helix_video = ctx.helix_video

    # ── VOD rows ──────────────────────────────────────────────────────────

    @app.post("/admin/generate/vod", dependencies=auth)
    async def generate_vod(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId")
        if await vod_exists(body["vodId"]):
            raise AdminError(400, "This VOD is already in the archive")
        video = await helix_video(body["vodId"])
        await upsert_vod(video)
        await enqueue("chapters", video["id"])
        job = await enqueue("emotes", video["id"])
        return _ok(f"Created vod {video['id']}", job)

    @app.post("/admin/create", dependencies=auth)
    async def create_vod(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId", "title", "createdAt", "duration")
        if await vod_exists(body["vodId"]):
            raise AdminError(400, f"{body['vodId']} already exists!")
        created = dt.datetime.fromisoformat(str(body["createdAt"]))
        async with get_sessionmaker()() as s:
            s.add(
                Vod(
                    id=str(body["vodId"]),
                    title=body["title"],
                    created_at=created,
                    duration=body["duration"],
                    drive=[body["drive"]] if body.get("drive") else [],
                    platform=body.get("platform") or "twitch",
                )
            )
            await s.commit()
        return _ok(f"Created {body['vodId']} in vods DB!")

    @app.delete("/admin/delete", dependencies=auth)
    async def delete_vod(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId")
        vod_id = str(body["vodId"])
        await refuse_spliced(vod_id, "deleting it (with the chat rows it holds)")
        async with get_sessionmaker()() as s:
            for model in (Log, Emote):
                await s.execute(delete(model).where(model.vod_id == vod_id))
            await s.execute(delete(Vod).where(Vod.id == vod_id))
            await s.commit()
        return _ok(f"Deleted {vod_id} (vod, logs, emotes)")

    @app.post("/admin/duration", dependencies=auth)
    async def save_duration(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId")
        await require_vod(body["vodId"])
        await refuse_spliced(body["vodId"], "setting its duration from Twitch")
        video = await helix_video(body["vodId"])
        duration = format_hhmmss(parse_helix_duration(video.get("duration", "")))
        await save_vod(str(body["vodId"]), duration=duration)
        return _ok("Saved duration!", duration=duration)

    # ── The dashboard's read of a VOD ───────────────────────────────────────────

    async def save_vod(vod_id: str, **values: Any) -> None:
        """Update a VOD row (a database trigger tells archive-api to drop its cached copies).
        Hiding or showing it also drops its cached chat and emotes, which the trigger doesn't cover."""
        async with get_sessionmaker()() as s:
            res = await s.execute(update(Vod).where(Vod.id == vod_id).values(**values))
            if res.rowcount == 0:
                raise AdminError(404, "No such VOD")
            if "hidden" in values:
                await notify_rows_moved(s, vod_id)
            await s.commit()

    async def admin_vod(vod_id: str) -> dict[str, Any]:
        """The VOD as GET /vods/{id} renders it, plus what only the dashboard needs."""
        async with get_sessionmaker()() as s:
            vod = await vod_json(await s.connection(), vod_id)
            if vod is None:
                raise AdminError(404, "No such VOD")
            locked, bot_chat, hidden, censor = (
                await s.execute(
                    select(Vod.chapters_locked, Vod.bot_chat, Vod.hidden, Vod.censor).where(Vod.id == vod_id)
                )
            ).one()
            recent = (
                await s.execute(
                    select(ALL_JOBS)
                    .where(ALL_JOBS.c.vod_id == vod_id)
                    .order_by(ALL_JOBS.c.id.desc())
                    .limit(RECENT_JOBS)
                )
            ).all()
            recent = [_job_json(j) for j in recent]
        return {
            **vod,
            "hidden": hidden,
            "chaptersLocked": locked,
            "botChat": bot_chat,
            "censor": censor,
            "cut_reason": vod.get("cut_reason"),  # the public JSON leaves it out while NULL
            "jobs": recent,
            "splices": [],  # merges and splits are synthetic VODs now (ADR 0004); kept for the dashboard
        }

    @app.get("/admin/vods/{vod_id}", dependencies=auth)
    async def get_vod(vod_id: str) -> dict[str, Any]:
        return await admin_vod(vod_id)

    # ── Merging and splitting VODs: synthetic VODs, in /api/v2 (ADR 0004) ──

    @app.get("/admin/vods/{vod_id}/merge-candidates", dependencies=auth)
    async def merge_candidates(vod_id: str) -> dict[str, Any]:
        """VODs that started within ARCHIVE_MERGE_CANDIDATE_MINUTES after this one ended."""
        found = await synthetic.merge_candidates(vod_id, settings.merge_candidate_minutes)
        if found is None:
            raise AdminError(404, "No Vod Data")
        a, rows = found
        duration = hhmmss_to_seconds(a.duration)
        ends = a.created_at + dt.timedelta(seconds=duration)
        candidates = []
        for v in rows:
            gap = round((v.created_at - a.created_at).total_seconds()) - duration
            candidates.append(
                {
                    "id": v.id,
                    "streamId": v.stream_id,
                    "title": v.title,
                    "createdAt": iso_utc(v.created_at),
                    "duration": v.duration,
                    "gap": gap,
                    "overlaps": gap < 0,
                    "titlesMatch": synthetic.normal_title(v.title) == synthetic.normal_title(a.title),
                }
            )
        return {
            "vod": {
                "id": a.id,
                "streamId": a.stream_id,
                "title": a.title,
                "createdAt": iso_utc(a.created_at),
                "duration": a.duration,
                "endsAt": iso_utc(ends),
                "mergedInto": a.merged_into,
            },
            "withinMinutes": settings.merge_candidate_minutes,
            "candidates": candidates,
        }
