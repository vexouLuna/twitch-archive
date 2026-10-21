"""VOD rows and their editing: games, chapters, uploads, censor ranges, merging and splitting."""

from __future__ import annotations

import datetime as dt
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import Emote, Game, Log, Vod
from archive_common.serialize import EMOTES, GAMES, duration_seconds, vod_json
from archive_common.timeutil import format_hhmmss, hhmmss_to_seconds, parse_helix_duration
from fastapi import Body, FastAPI, Request
from sqlalchemy import delete, func, insert, or_, select, tuple_, update

from . import (
    jobs,
    site_tags,
    splices,
    vod_edits,
    youtube,
)
from .admin_shared import RECENT_JOBS, VOD_LIST_MAX, AdminContext, AdminError, _actor, _job_json, _ok, _require, log
from .events import iso_utc
from .job_rows import ALL_JOBS
from .steps import content_check
from .vods import drop_youtube_entries, notify_rows_moved, upsert_vod


def register(app: FastAPI, ctx: AdminContext) -> None:
    deps = ctx.deps
    settings = ctx.settings
    service = ctx.service
    auth = ctx.auth
    enqueue = ctx.enqueue
    job_action = ctx.job_action
    vod_exists = ctx.vod_exists
    require_vod = ctx.require_vod
    refuse_spliced = ctx.refuse_spliced
    helix_video = ctx.helix_video
    type_payload = ctx.type_payload
    source_payload = ctx.source_payload
    single_part = ctx.single_part

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
            for model in (Log, Emote, Game):
                await s.execute(delete(model).where(model.vod_id == vod_id))
            await s.execute(delete(Vod).where(Vod.id == vod_id))
            await s.commit()
        return _ok(f"Deleted {vod_id} (vod, logs, emotes, games)")

    @app.post("/admin/duration", dependencies=auth)
    async def save_duration(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        _require(body, "vodId")
        await require_vod(body["vodId"])
        await refuse_spliced(body["vodId"], "setting its duration from Twitch")
        video = await helix_video(body["vodId"])
        duration = format_hhmmss(parse_helix_duration(video.get("duration", "")))
        await save_vod(str(body["vodId"]), duration=duration)
        return _ok("Saved duration!", duration=duration)

    # ── VOD editing (dashboard) ───────────────────────────────────────────

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
            "splices": await splices.active_splices(vod_id),
        }

    def edited(parse, *args) -> Any:  # type: ignore[no-untyped-def]
        try:
            return parse(*args)
        except ValueError as exc:
            raise AdminError(400, str(exc)) from exc

    def refuse_merged(vod: Vod, what: str) -> None:
        if vod.merged_into is not None:
            raise AdminError(
                409,
                f"{vod.id} was merged into {vod.merged_into.get('id')}; its {what} are that VOD's "
                "now. Undo the merge first",
            )

    def vod_row_json(vod: Vod) -> dict[str, Any]:
        """A row of GET /admin/vods, and the fields PATCH changes (named as the public API names them)."""
        return {
            "id": vod.id,
            "title": vod.title,
            "createdAt": iso_utc(vod.created_at),
            "duration": vod.duration,
            "duration_seconds": duration_seconds(vod.duration),
            "thumbnail_url": vod.thumbnail_url,
            "stream_id": vod.stream_id,
            "hidden": vod.hidden,
            "merged_into": vod.merged_into,
            "tags": list(vod.tags or []),
            "cut_reason": vod.cut_reason,
        }

    def fields_json(vod: Vod, keys) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        row = vod_row_json(vod)
        return {k: row[{"thumbnailUrl": "thumbnail_url", "cutReason": "cut_reason"}.get(k, k)] for k in keys}

    @app.get("/admin/vods", dependencies=auth)
    async def list_vods(
        q: str = "", hidden: bool | None = None, limit: int = 50, before: str | None = None
    ) -> dict[str, Any]:
        """Every VOD, hidden and merged ones too, newest first. ``q`` matches the id exactly or the title
        (case-insensitive substring); ``hidden`` filters; ``before`` (a VOD id, from ``next``) pages back."""
        limit = min(max(limit, 1), VOD_LIST_MAX)
        stmt = select(Vod).order_by(Vod.created_at.desc(), Vod.id.desc()).limit(limit + 1)
        if q.strip():
            term = q.strip()
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(or_(Vod.id == term, Vod.title.ilike(like, escape="\\")))
        if hidden is not None:
            stmt = stmt.where(Vod.hidden.is_(hidden))
        async with get_sessionmaker()() as s:
            if before:
                after = await s.get(Vod, before)
                if after is None:
                    raise AdminError(400, f"before: no VOD {before}")
                stmt = stmt.where(tuple_(Vod.created_at, Vod.id) < tuple_(after.created_at, after.id))
            vods = (await s.execute(stmt)).scalars().all()
        more = len(vods) > limit
        vods = vods[:limit]
        return {"data": [vod_row_json(v) for v in vods], "next": vods[-1].id if more else None}

    @app.get("/admin/vods/{vod_id}", dependencies=auth)
    async def get_vod(vod_id: str) -> dict[str, Any]:
        return await admin_vod(vod_id)

    @app.patch("/admin/vods/{vod_id}", dependencies=auth)
    async def patch_vod(vod_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Any of ``title``, ``hidden``, ``thumbnailUrl`` (null: the default), ``duration`` (HH:MM:SS),
        ``createdAt`` (ISO), ``tags`` (the site's, site_tags.vod_tags) and ``cutReason`` (text, null: none).
        A merged VOD takes only ``hidden`` and ``tags``. Audited with the fields before and after."""
        vod = await require_vod(vod_id)
        values = edited(vod_edits.vod_fields, body, await known_tags(vod) if "tags" in body else ())
        if set(body) - vod_edits.MERGED_EDITABLE:
            refuse_merged(vod, "contents")
        if "duration" in values:
            await check_fits(vod, hhmmss_to_seconds(values["duration"]))
        before = fields_json(vod, body)
        if values:
            await save_vod(vod.id, **values)
        after = await admin_vod(vod.id)
        request.state.audit_detail = {"before": before, "after": fields_json(await require_vod(vod.id), body)}
        return after

    async def known_tags(vod: Vod) -> tuple[str, ...]:
        async with get_sessionmaker()() as s:
            return await site_tags.vod_tags(s, vod.tags)

    async def check_fits(vod: Vod, seconds: float) -> None:
        """A new duration must still hold the VOD's chapters and games rows."""
        async with get_sessionmaker()() as s:
            games_end = (await s.execute(select(func.max(Game.end_time)).where(Game.vod_id == vod.id))).scalar()
        edited(vod_edits.check_fits, vod.chapters, float(games_end or 0), seconds)

    async def games_json(vod_id: str) -> list[dict[str, Any]]:
        async with get_sessionmaker()() as s:
            rows = await s.execute(
                select(*GAMES.columns())
                .where(GAMES.table.c.vod_id == vod_id)
                .order_by(GAMES.table.c.start_time, GAMES.table.c.id)
            )
        return [GAMES.to_json(r) for r in rows.mappings()]

    def game_fields(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The editable part of each games row (for the audit)."""
        keep = ("start_time", "end_time", *vod_edits.GAME_TEXT, *vod_edits.GAME_URLS)
        return [{k: r.get(k) for k in keep} for r in rows]

    @app.get("/admin/vods/{vod_id}/games", dependencies=auth)
    async def get_games(vod_id: str) -> list[dict[str, Any]]:
        """The VOD's games rows as GET /games renders them, by start."""
        await require_vod(vod_id)
        return await games_json(vod_id)

    @app.put("/admin/vods/{vod_id}/games", dependencies=auth)
    async def put_games(vod_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Replace the games rows: ``{"games": [...]}`` in the shape GET returns (ids and dates ignored)."""
        vod = await require_vod(vod_id)
        refuse_merged(vod, "games rows")
        rows = edited(vod_edits.games, body.get("games"), hhmmss_to_seconds(vod.duration))
        before = await games_json(vod.id)
        async with get_sessionmaker()() as s:
            await s.execute(delete(Game).where(Game.vod_id == vod.id))
            if rows:
                await s.execute(insert(Game), [{"vod_id": vod.id, **r} for r in rows])
            await s.commit()
        request.state.audit_detail = {"before": game_fields(before), "after": game_fields(await games_json(vod.id))}
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/chapters", dependencies=auth)
    async def put_chapters(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Replace the chapters. ``locked: true`` keeps the automatic chapters step from
        overwriting them (unless that job's payload has ``"force": true``)."""
        vod = await require_vod(vod_id)
        if not isinstance(body.get("locked"), bool):
            raise AdminError(400, "locked must be true or false")
        chapters = edited(vod_edits.chapters, body.get("chapters"), hhmmss_to_seconds(vod.duration))
        await save_vod(vod.id, chapters=chapters, chapters_locked=body["locked"])
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/youtube", dependencies=auth)
    async def put_youtube(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        vod = await require_vod(vod_id)
        await save_vod(vod.id, youtube=edited(vod_edits.youtube, body.get("youtube"), vod.youtube))
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/drive", dependencies=auth)
    async def put_drive(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        vod = await require_vod(vod_id)
        await save_vod(vod.id, drive=edited(vod_edits.drive, body.get("drive")))
        return await admin_vod(vod.id)

    @app.put("/admin/vods/{vod_id}/censor", dependencies=auth)
    async def put_censor(vod_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Replace the ranges blurred, blacked out or muted in the uploads (vod_edits.censor). Uploads made from
        now on carry them; re-archive the VOD to replace the ones already on YouTube."""
        vod = await require_vod(vod_id)
        refuse_merged(vod, "uploads")
        if vod.synthetic is not None:
            raise AdminError(409, f"{vod.id} is synthetic: it has no uploads of its own to edit")
        ranges = edited(vod_edits.censor, body.get("censor"), hhmmss_to_seconds(vod.duration))
        before = list(vod.censor or [])
        await save_vod(vod.id, censor=ranges)
        request.state.audit_detail = {"before": before, "after": ranges}
        return await admin_vod(vod.id)

    @app.post("/admin/jobs/{job_id}/censor-review", dependencies=auth)
    async def censor_review(job_id: int, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Answer a job the ``censor_check`` step paused: ``{"decision": "upload"}`` resumes it (with whatever
        censor ranges are set by now), ``"cut"`` first marks the flagged games' chapters restricted (and locks
        the chapters) so ``split`` leaves them out of the uploads."""
        decision = body.get("decision")
        if decision not in ("upload", "cut"):
            raise AdminError(400, 'decision must be "upload" or "cut"')
        job = await jobs.get(job_id)
        review = (job.payload or {}).get("censor_review") or {}
        if job.state != "paused" or not review.get("games"):
            raise AdminError(409, f"Job {job_id} is not paused for a censor review")
        if decision == "cut":
            ids = {str(g["gameId"]) for g in review["games"] if g.get("gameId")}
            if not ids:
                raise AdminError(409, "IGDB could not be checked, so there are no games to cut; set censor ranges")
            vod = await require_vod(str(job.vod_id))
            chapters = content_check.cut_games(vod.chapters, ids)
            await save_vod(vod.id, chapters=chapters, chapters_locked=True)
            request.state.audit_detail = {"vod": vod.id, "cut": sorted(ids)}
        job = await service.resume(job_id, actor=_actor.get())
        return job_action(f"Job {job_id} resumed at step {job.step} ({decision})", job)

    @app.post("/admin/vods/{vod_id}/youtube/delete", dependencies=auth)
    async def delete_uploads(vod_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Delete ``videos`` (ids from the VOD's ``youtube`` list) from YouTube, 50 quota units each, and take
        them off the list; one already gone from YouTube is just taken off. The remaining parts' descriptions
        are rewritten (a describe job per type) so they stop linking to the deleted ones."""
        vod = await require_vod(vod_id)
        ids = body.get("videos")
        if not isinstance(ids, list) or not ids or not all(isinstance(v, str) and v for v in ids):
            raise AdminError(400, "videos must be a list of YouTube video ids")
        listed = {str(e.get("id")): e for e in vod.youtube or []}
        unknown = [v for v in ids if v not in listed]
        if unknown:
            raise AdminError(400, f"Not uploads of {vod.id}: {', '.join(unknown)}")
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        for video_id in dict.fromkeys(ids):
            try:
                await deps.youtube.delete(video_id)
            except youtube.YouTubeNotAuthorized as exc:
                raise AdminError(409, str(exc)) from exc
            except Exception as exc:
                log.warning("could not delete %s from YouTube: %s", video_id, exc)
                failed.append({"id": video_id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            deleted.append(video_id)
        dropped = await drop_youtube_entries(vod.id, set(deleted))
        after = await require_vod(vod.id)
        described = []
        for typ in sorted({e.get("type") or "vod" for e in dropped}):
            if any((e.get("type") or "vod") == typ for e in after.youtube or []):
                described.append((await enqueue("describe", vod.id, type_payload(after, {"type": typ}))).id)
        request.state.audit_detail = {"deleted": dropped, "failed": failed}
        msg = f"Deleted {len(deleted)} upload(s) from YouTube" + (f", {len(failed)} failed" if failed else "")
        return {**_ok(msg, deleted=deleted, failed=failed, describeJobs=described), "vod": await admin_vod(vod.id)}

    @app.post("/admin/vods/{vod_id}/rearchive", dependencies=auth)
    async def rearchive(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Upload the VOD (``type`` vod or live; one ``part``, or all) again with today's settings and censor
        ranges, deleting each upload it replaces from YouTube once the new one is in place: a ``download``
        job (``reupload`` for one part) with ``replace``. The source is a local copy if there is one
        (or ``path``), else the VOD is downloaded from Twitch again."""
        vod = await require_vod(vod_id)
        refuse_merged(vod, "uploads")
        if vod.synthetic is not None:
            raise AdminError(409, f"{vod.id} is synthetic: it has no uploads of its own")
        payload = {**source_payload(vod, body), "replace": True}
        if body.get("part") not in (None, ""):
            part = single_part(payload, body)
            job = await enqueue("reupload", vod.id, payload)
            return _ok(f"Re-archiving {vod.id} part {part}; the old upload goes once the new one is up", job)
        job = await enqueue("download", vod.id, payload)
        return _ok(f"Re-archiving {vod.id}; the old uploads go once the new ones are up", job)

    @app.get("/admin/vods/{vod_id}/emotes", dependencies=auth)
    async def get_vod_emotes(vod_id: str) -> dict[str, Any] | None:
        """The saved emotes row as GET /emotes renders it, or null if none was saved."""
        await require_vod(vod_id)
        async with get_sessionmaker()() as s:
            row = (await s.execute(select(*EMOTES.columns()).where(EMOTES.table.c.vod_id == vod_id))).mappings().first()
        return EMOTES.to_json(row) if row else None

    # ── Merging and splitting VODs (see splices) ─────────────────────────

    def force(body: dict[str, Any]) -> bool:
        if body.get("force") not in (None, True, False):
            raise AdminError(400, "force must be true or false")
        return body.get("force") is True

    @app.get("/admin/vods/{vod_id}/merge-candidates", dependencies=auth)
    async def merge_candidates(vod_id: str) -> dict[str, Any]:
        """VODs that started within ARCHIVE_MERGE_CANDIDATE_MINUTES after this one ended."""
        return await splices.merge_candidates(vod_id, settings.merge_candidate_minutes)

    @app.post("/admin/vods/{vod_id}/merge", dependencies=auth)
    async def merge_vods(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """``{"source": id, "gap"?: seconds}``: append ``source`` (the later VOD) to this one."""
        _require(body, "source")
        source = str(body["source"])
        result = await splices.merge(vod_id, source, body.get("gap"))
        return _ok(
            f"Merged {source} into {vod_id} at {result['splice']['offset']}s", **result, vod=await admin_vod(vod_id)
        )

    @app.post("/admin/vods/{vod_id}/unmerge", dependencies=auth)
    async def unmerge_vods(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """``{"source": id, "force"?: true}``: restore both VODs as they were before the merge."""
        _require(body, "source")
        source = str(body["source"])
        result = await splices.unmerge(vod_id, source, force(body))
        return _ok(f"Unmerged {source} from {vod_id}", **result, vod=await admin_vod(vod_id))

    @app.post("/admin/vods/{vod_id}/split", dependencies=auth)
    async def split_vod(vod_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """``{"at": seconds}``: the rest of the VOD from ``at`` becomes a new VOD, or, at a
        merge's join, that merge is undone. 409 with ``validPoints`` inside an upload."""
        _require(body, "at")
        result = await splices.split(vod_id, body["at"], force(body))
        if result.get("undid") == "merge":
            msg = f"{body['at']}s is where {result['splice']['otherId']} was merged in; undid that merge"
        else:
            msg = f"Split {vod_id} at {result['splice']['offset']}s; the rest is {result['newVodId']}"
        return _ok(msg, **result, vod=await admin_vod(vod_id))

    @app.post("/admin/vods/{vod_id}/unsplit", dependencies=auth)
    async def unsplit_vod(vod_id: str, body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """``{"source"?: second half's id, "force"?: true}``: join the halves again (default: the latest split)."""
        body = body or {}
        other = str(body["source"]) if body.get("source") not in (None, "") else None
        result = await splices.unsplit(vod_id, other, force(body))
        return _ok(f"Joined {result['splice']['otherId']} back into {vod_id}", **result, vod=await admin_vod(vod_id))
