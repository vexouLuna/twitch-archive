"""/api/v2's own routes (api_v2.py mounts them): settings, storage and VODs.

The same changes as their /admin routes, in the v2 shapes (snake_case, ``{items, next_cursor}``,
problem details). Each change writes its audit row itself: in the change's transaction where there is
one (settings, VODs), right after it otherwise (a storage delete is files, not rows).

    GET    /api/v2/settings            every runtime setting
    PATCH  /api/v2/settings            {key: value, ...}: all of them or none
    DELETE /api/v2/settings/{key}      back to the env value
    GET    /api/v2/storage             the disk and each job folder
    DELETE /api/v2/storage/{area}/{name}
    GET    /api/v2/vods                every VOD, hidden, merged and synthetic ones too, newest first, each
                                       with whether it is uploaded and its queued, running or paused jobs
    GET    /api/v2/vods/{vod_id}
    PATCH  /api/v2/vods/{vod_id}       title, hidden, thumbnail_url, duration, created_at, tags, cut_reason
    GET    /api/v2/vods/{vod_id}/games its games rows, by start
    PUT    /api/v2/vods/{vod_id}/games {games}: all of them replaced
    PUT    /api/v2/vods/{vod_id}/chapters {chapters, locked}: all of them replaced
    PUT    /api/v2/vods/{vod_id}/youtube {youtube}, /drive {drive}, /censor {censor}: each list replaced
    GET    /api/v2/vods/{vod_id}/emotes  the saved emotes row, or null
    POST   /api/v2/vods/{vod_id}/rearchive {type, part?, path?}: upload it again, replacing the old uploads
    POST   /api/v2/vods/{vod_id}/youtube/delete {videos}: off YouTube and off its list
    POST   /api/v2/jobs/{job_id}/censor-review {decision}: answer a job the censor check paused
    POST   /api/v2/synthetic           a synthetic VOD: segments of real ones (see compose, synthetic)
    GET    /api/v2/synthetic/{vod_id}
    PUT    /api/v2/synthetic/{vod_id}  segments, title, tags (supersedes: legacy, see synthetic.py)
    DELETE /api/v2/synthetic/{vod_id}  the whole undo: its sources were never changed
    GET    /api/v2/synthetic/{vod_id}/dmca  the DMCA claims on its sources: {overlapping, other}
    GET    /api/v2/vods/{vod_id}/merge-candidates?hours=  VODs that started soon after it ended (or, with
                                       hours, ended soon before it started), oldest first
    POST   /api/v2/vods/{vod_id}/merge {sources, gaps?, id?}: one synthetic VOD of them all, hiding them
    POST   /api/v2/vods/{vod_id}/split {at, ids?}: two synthetic VODs, hiding it
    GET    /api/v2/playthrough-candidates?game_id=  where real VODs play that game, oldest first
"""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from archive_common import audit
from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import Game, Vod
from archive_common.segments import Segment
from archive_common.serialize import EMOTES, duration_seconds, real, tagged
from archive_common.timeutil import hhmmss_to_seconds
from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import Field
from sqlalchemy import delete, exists, func, insert, or_, select, tuple_, update
from vex_platform.actor import Actor
from vex_platform.api import ApiError, ApiModel, Page, UtcDatetime, decode_cursor, page_of, request_id, utc_iso
from vex_platform.audit import AuditEntry
from vex_platform.audit.sqlalchemy import record

from . import compose, jobs, site_tags, synthetic, vod_edits, youtube
from .admin_shared import AdminContext, AdminError, log
from .job_rows import ACTIVE, ALL_JOBS
from .runtime_settings import RuntimeSettings
from .steps import content_check
from .storage import Storage, StorageError
from .vods import drop_youtube_entries, notify_rows_moved

Auth = Callable[[Request], Any]
VOD_LIST_MAX = 200


def _actor(request: Request) -> Actor:
    return request.state.actor


def _changed_by(actor: Actor) -> str:
    """``settings.updated_by``, as the v1 routes write it: the login, else the caller."""
    return actor.login or audit.legacy_actor(actor.kind, actor.id)


def _snake(value: Any, deep: bool = True) -> Any:
    """v1's camelCase keys, snake_case (recursively unless ``deep`` is false)."""
    if isinstance(value, dict):
        return {
            "".join(f"_{c.lower()}" if c.isupper() else c for c in k): _snake(v) if deep else v
            for k, v in value.items()
        }
    if isinstance(value, list) and deep:
        return [_snake(v) for v in value]
    return value


# ── Settings ───────────────────────────────────────────────────────────────


class Setting(ApiModel):
    key: str
    value: Any
    default: Any
    overridden: bool
    type: str
    group: str
    applies: str
    help: str
    min: float | None = None
    max: float | None = None
    updated_at: UtcDatetime | None = None
    updated_by: str | None = None
    choices: dict[str, list[str]] | None = None  # the job kinds' steps, for a "steps" setting
    requires: str | None = None  # a bool setting this one does nothing without (disable it while that is off)


def settings_router(runtime: RuntimeSettings, apply: Callable[[], None], auth: Auth) -> APIRouter:
    """``apply``: hands changed settings to the job runtime (concurrency, attempts, gates)."""
    router = APIRouter(tags=["settings"], dependencies=[Depends(auth)])

    def page() -> Page[Setting]:
        # Only the keys: a value may be a dict of job kinds.
        return Page[Setting](items=[Setting(**_snake(item, deep=False)) for item in runtime.describe()])

    @router.get("/settings")
    async def list_settings() -> Page[Setting]:
        """Each setting the dashboard can change: value, env default, whether overridden, when it applies."""
        return page()

    @router.patch("/settings")
    async def update_settings(request: Request, changes: Annotated[dict[str, Any], Body()]) -> Page[Setting]:
        """``{key: value, ...}``: all of them, or none when one is refused. Audited as ``setting.update``."""
        actor = _actor(request)
        try:
            await runtime.update(
                changes,
                _changed_by(actor),
                lambda before, after: AuditEntry(
                    "setting.update", actor, before=before, after=after, request_id=request_id(request)
                ),
            )
        except ValueError as exc:
            raise ApiError(422, "invalid_setting", str(exc)) from None
        apply()
        return page()

    @router.delete("/settings/{key}")
    async def reset_setting(request: Request, key: str) -> Page[Setting]:
        """Back to the env value (or the default). Audited as ``setting.reset``."""
        actor = _actor(request)
        try:
            await runtime.reset(
                key,
                lambda before, after: AuditEntry(
                    "setting.reset", actor, f"setting:{key}", before=before, after=after, request_id=request_id(request)
                ),
            )
        except KeyError:
            raise ApiError(404, "setting_not_found", f"no setting {key}") from None
        apply()
        return page()

    return router


# ── Storage ────────────────────────────────────────────────────────────────


class StorageJob(ApiModel):
    id: int
    kind: str
    state: str
    step: str | None = None
    updated_at: UtcDatetime | None = None


class FolderJobs(ApiModel):
    active: list[StorageJob]
    last: StorageJob | None = None


class FolderVod(ApiModel):
    id: str
    title: str | None = None
    hidden: bool


class Folder(ApiModel):
    area: str
    name: str
    path: str
    bytes: int
    files: int
    modified_at: UtcDatetime | None = None
    vod: FolderVod | None = None
    jobs: FolderJobs
    stale: bool  # no job working in it, and no VOD or its last job failed or was cancelled


class Disk(ApiModel):
    total: int
    used: int
    free: int


class StorageView(ApiModel):
    disk: Disk | None = None
    folders: list[Folder]
    cache_seconds: float


class Freed(ApiModel):
    path: str
    bytes: int
    files: int


def storage_router(storage: Storage, auth: Auth) -> APIRouter:
    router = APIRouter(tags=["storage"], dependencies=[Depends(auth)])

    @router.get("/storage")
    async def get_storage(refresh: bool = False) -> StorageView:
        """The disk, and each job folder (vods/<id>, live/<stream id>) with its size, VOD and jobs.
        Sizes are cached for a short while; ``refresh=true`` scans again."""
        return StorageView(**_snake(await storage.view(refresh)))

    @router.delete("/storage/{area}/{name}")
    async def delete_storage(request: Request, area: str, name: str) -> Freed:
        """Delete a folder's files; refused while a job for it is queued, running or paused.
        Audited as ``storage.delete`` with what it freed."""
        try:
            freed = await storage.delete(area, name)
        except StorageError as exc:
            code = {404: "folder_not_found", 409: "folder_in_use"}.get(exc.status)
            raise ApiError(exc.status, code, exc.msg) from None
        await audit.write(
            AuditEntry(
                "storage.delete",
                _actor(request),
                f"storage:{area}/{name}",
                detail=freed,
                request_id=request_id(request),
            )
        )
        return Freed(**freed)

    return router


# ── VODs ───────────────────────────────────────────────────────────────────


class ActiveJob(ApiModel):
    id: int
    kind: str
    state: str  # queued, running or paused
    step: str | None = None


class VodRow(ApiModel):
    id: str
    title: str | None = None
    created_at: UtcDatetime
    duration: str | None = None
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    stream_id: str | None = None
    hidden: bool
    merged_into: dict[str, Any] | None = None  # {id, offset} once merged into that VOD
    tags: list[str]  # [] = a regular VOD
    synthetic: dict[str, Any] | None = None  # {supersedes} on a synthetic VOD
    cut_reason: str | None = None  # why the VOD was cut, when an admin gave a reason
    uploaded: bool = False  # it has YouTube parts
    active_jobs: list[ActiveJob] = []  # queued, running or paused, oldest first


class VodDetail(VodRow):
    platform: str
    chapters: list[dict[str, Any]]
    chapters_locked: bool  # edited by hand: the chapters step leaves them alone
    youtube: list[dict[str, Any]]
    drive: list[dict[str, Any]]
    censor: list[dict[str, Any]]  # ranges blurred, blacked out or muted in uploads made from now on
    bot_chat: dict[str, Any] | None = None  # the last bot chat read
    updated_at: UtcDatetime
    segments: list[dict[str, Any]] | None = None  # a synthetic VOD's
    in_synthetic: list[dict[str, Any]]  # the synthetic VODs made with it: {id, title, tags, supersedes, segments}


class VodPatch(ApiModel):
    """Only the fields sent change. ``thumbnail_url: null`` goes back to the default; a merged VOD
    takes only ``hidden`` and ``tags``, a synthetic one only ``title``, ``hidden``, ``thumbnail_url``, ``tags`` and
    ``cut_reason`` (its own thumbnail is kept over its first source's until set back to null)."""

    title: str | None = None
    hidden: bool | None = None
    thumbnail_url: str | None = None
    duration: str | None = None  # HH:MM:SS
    created_at: str | None = None  # ISO 8601 with an offset
    tags: list[str] | None = None
    cut_reason: str | None = None  # 500 characters at most; null for none


class GameRow(ApiModel):
    """A ``games`` row: a stretch of the VOD playing one game (what the site's game list shows)."""

    id: str
    vod_id: str
    start_time: float  # seconds into the VOD
    end_time: float
    game_id: str | None = None
    game_name: str | None = None
    title: str | None = None
    video_provider: str | None = None
    video_id: str | None = None
    thumbnail_url: str | None = None
    chapter_image: str | None = None
    created_at: UtcDatetime | None = None
    updated_at: UtcDatetime | None = None


class GameIn(ApiModel):
    """A games row to store. ``id``, ``vod_id``, ``created_at`` and ``updated_at`` are taken and ignored,
    so the items GET returns can be sent back as they are."""

    start_time: float
    end_time: float
    game_name: str | None
    game_id: str | None = None
    title: str | None = None
    video_provider: str | None = None
    video_id: str | None = None
    thumbnail_url: str | None = None
    chapter_image: str | None = None
    id: str | None = None
    vod_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


_GAME_READ_ONLY = {"id", "vod_id", "created_at", "updated_at"}


class GamesPut(ApiModel):
    games: list[GameIn]


class ChaptersPut(ApiModel):
    """Every chapter, as ``/admin`` takes them: ``{name, gameId, start, length, restricted}``, plus
    ``imageTemplate`` and ``kind: "gap"`` if wanted. They are stored (and returned) in the legacy shape,
    where ``start`` and ``end`` are the start and the length. ``locked: true`` keeps the automatic
    chapters step from overwriting them (unless that job's payload has ``"force": true``)."""

    chapters: list[dict[str, Any]]
    locked: bool


class MergeCandidate(ApiModel):
    """A VOD that started soon after this one ended, or (``?hours=``) ended soon before it started:
    ``gap`` seconds from the earlier one's end to the later one's start (negative: they overlap)."""

    id: str
    stream_id: str | None = None
    title: str | None = None
    created_at: UtcDatetime
    duration: str | None = None
    gap: float
    titles_match: bool  # the same title, ignoring case and spacing: likely the same broadcast


class YoutubePut(ApiModel):
    """Every upload: ``{id, type ("vod" or "live"), part, duration?}``. A video already listed keeps its
    thumbnail, seek-bar previews and recording gaps."""

    youtube: list[dict[str, Any]]


class DrivePut(ApiModel):
    """Every Drive file: ``{id, type}``."""

    drive: list[dict[str, Any]]


class CensorPut(ApiModel):
    """Every range: ``{start, end, video ("blur", "black", "pixelate", "color" or null), color?, region?, mute,
    note?}`` in VOD seconds (docs/admin-api.md). Uploads
    made from now on carry them; re-archive the VOD to replace the ones already on YouTube."""

    censor: list[dict[str, Any]]


def _game(row: Game) -> GameRow:
    return GameRow(
        id=str(row.id),
        vod_id=row.vod_id,
        start_time=float(row.start_time or 0),
        end_time=float(row.end_time or 0),
        game_id=row.game_id,
        game_name=row.game_name,
        title=row.title,
        video_provider=row.video_provider,
        video_id=row.video_id,
        thumbnail_url=row.thumbnail_url,
        chapter_image=row.chapter_image,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _games_audited(rows: list[GameRow]) -> list[dict[str, Any]]:
    """The editable part of each row, as the audit records it."""
    return [r.model_dump(exclude=_GAME_READ_ONLY) for r in rows]


async def _games(s: Any, vod_id: str) -> list[GameRow]:
    rows = await s.execute(select(Game).where(Game.vod_id == vod_id).order_by(Game.start_time, Game.id))
    return [_game(r) for r in rows.scalars()]


async def _locked_vod(s: Any, vod_id: str) -> Vod:
    vod = await s.get(Vod, vod_id, with_for_update=True)
    if vod is None:
        raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
    return vod  # type: ignore[no-any-return]


# VodPatch field -> vod_edits.vod_fields' key.
_V1_FIELDS = {"thumbnail_url": "thumbnailUrl", "created_at": "createdAt", "cut_reason": "cutReason"}


def _row(vod: Vod) -> dict[str, Any]:
    return {
        "id": vod.id,
        "title": vod.title,
        "created_at": vod.created_at,
        "duration": vod.duration,
        "duration_seconds": duration_seconds(vod.duration),
        "thumbnail_url": vod.thumbnail_url,
        "stream_id": vod.stream_id,
        "hidden": vod.hidden,
        "merged_into": vod.merged_into,
        "tags": list(vod.tags or []),
        "synthetic": vod.synthetic,
        "cut_reason": vod.cut_reason,
        "uploaded": bool(vod.youtube),
    }


def _active(rows: list[Any]) -> list[ActiveJob]:
    return [ActiveJob(id=j.id, kind=j.kind, state=j.state, step=j.step) for j in rows]


async def _detail(vod: Vod) -> VodDetail:
    segments = (await synthetic.get(vod.id))["segments"] if vod.synthetic is not None else None
    return VodDetail(
        **_row(vod),
        platform=vod.platform,
        chapters=vod.chapters or [],
        chapters_locked=vod.chapters_locked,
        youtube=vod.youtube or [],
        drive=vod.drive or [],
        censor=vod.censor or [],
        bot_chat=vod.bot_chat,
        updated_at=vod.updated_at,
        active_jobs=_active((await jobs.active_by_vod([vod.id])).get(vod.id, [])),
        segments=_snake(segments) if segments is not None else None,
        in_synthetic=_snake(await synthetic.containing(vod.id)),
    )


def _audited(vod: Vod, fields: set[str]) -> dict[str, Any]:
    row = _row(vod)
    return {k: utc_iso(row[k]) if k == "created_at" else row[k] for k in sorted(fields)}


def vods_router(auth: Auth, settings: Settings) -> APIRouter:
    """``settings.merge_candidate_minutes``: how long after a VOD ends a merge candidate may start."""
    router = APIRouter(tags=["vods"], dependencies=[Depends(auth)])

    @router.get("/vods")
    async def list_vods(
        q: str = "",
        hidden: bool | None = None,
        tag: str | None = None,
        is_synthetic: Annotated[bool | None, Query(alias="synthetic")] = None,
        uploaded: bool | None = None,
        busy: bool | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=VOD_LIST_MAX)] = 50,
    ) -> Page[VodRow]:
        """``q`` matches the id exactly or the title (case-insensitive substring); ``hidden`` and
        ``synthetic`` filter; ``tag`` keeps the VODs with that tag (``tag=`` empty: untagged ones);
        ``uploaded`` is whether any part is on YouTube, ``busy`` whether a job is queued, running or
        paused for it (``uploaded=false&busy=false``: the VODs nothing is archiving)."""
        stmt = select(Vod).order_by(Vod.created_at.desc(), Vod.id.desc()).limit(limit + 1)
        if term := q.strip():
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(or_(Vod.id == term, Vod.title.ilike(like, escape="\\")))
        if hidden is not None:
            stmt = stmt.where(Vod.hidden.is_(hidden))
        if tag is not None:
            stmt = stmt.where(tagged(tag or None))
        if is_synthetic is not None:
            stmt = stmt.where(~real() if is_synthetic else real())
        if uploaded is not None:
            on_youtube = func.coalesce(func.jsonb_array_length(Vod.youtube), 0) > 0
            stmt = stmt.where(on_youtube if uploaded else ~on_youtube)
        if busy is not None:
            has_job = exists().where(ALL_JOBS.c.vod_id == Vod.id, ALL_JOBS.c.state.in_(ACTIVE))
            stmt = stmt.where(has_job if busy else ~has_job)
        if key := decode_cursor(cursor, size=2):
            try:
                at = dt.datetime.fromisoformat(key[0])
            except (TypeError, ValueError):
                raise ApiError(400, "bad_cursor", "The cursor is not one this API issued.") from None
            stmt = stmt.where(tuple_(Vod.created_at, Vod.id) < tuple_(at, str(key[1])))
        async with get_sessionmaker()() as s:
            vods = (await s.execute(stmt)).scalars().all()
        active = await jobs.active_by_vod([v.id for v in vods[:limit]])
        rows = [VodRow(**_row(v), active_jobs=_active(active.get(v.id, []))) for v in vods]
        page = page_of(rows, limit, lambda v: [v.created_at.isoformat(), v.id])
        return Page[VodRow](items=page.items, next_cursor=page.next_cursor)

    @router.get("/vods/{vod_id}")
    async def get_vod(vod_id: str) -> VodDetail:
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, vod_id)
        if vod is None:
            raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
        return await _detail(vod)

    @router.patch("/vods/{vod_id}")
    async def update_vod(request: Request, vod_id: str, patch: VodPatch) -> VodDetail:
        """Audited as ``vod.update`` with the fields sent, before and after."""
        sent = patch.model_dump(include=patch.model_fields_set)
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, vod_id, with_for_update=True)
            if vod is None:
                raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
            known = await site_tags.vod_tags(s, vod.tags) if "tags" in sent else ()
            v1 = {_V1_FIELDS.get(k, k): v for k, v in sent.items()}
            try:
                values = vod_edits.vod_fields(v1, known)
            except ValueError as exc:
                raise ApiError(422, "invalid_vod", str(exc)) from None
            if vod.merged_into is not None and set(v1) - vod_edits.MERGED_EDITABLE:
                raise ApiError(
                    409,
                    "vod_merged",
                    f"{vod.id} was merged into {vod.merged_into.get('id')}; its "
                    "contents are that VOD's now. Undo the merge first",
                )
            if vod.synthetic is not None and set(v1) - vod_edits.SYNTHETIC_EDITABLE:
                raise ApiError(
                    409,
                    "vod_synthetic",
                    f"{vod.id} is a synthetic VOD: its duration and date "
                    "come from its segments (PUT /api/v2/synthetic)",
                )
            if vod.synthetic is not None and "thumbnail_url" in values:
                values |= await synthetic.thumbnail(s, vod, values["thumbnail_url"])
            if "duration" in values:
                games_end = (await s.execute(select(func.max(Game.end_time)).where(Game.vod_id == vod.id))).scalar()
                try:
                    vod_edits.check_fits(vod.chapters, float(games_end or 0), hhmmss_to_seconds(values["duration"]))
                except ValueError as exc:
                    raise ApiError(422, "invalid_vod", str(exc)) from None
            before = _audited(vod, set(sent))
            if values:
                # A database trigger tells archive-api to drop its cached copies; hiding or showing it
                # also drops its cached chat and emotes, which the trigger doesn't cover.
                await s.execute(update(Vod).where(Vod.id == vod.id).values(**values))
                if "hidden" in values:
                    await notify_rows_moved(s, vod.id)
                await s.refresh(vod)
            await record(
                s,
                AuditEntry(
                    "vod.update",
                    _actor(request),
                    f"vod:{vod.id}",
                    before=before,
                    after=_audited(vod, set(sent)),
                    request_id=request_id(request),
                ),
                table=audit.TABLE,
            )
            await s.commit()
        return await _detail(vod)

    @router.get("/vods/{vod_id}/games")
    async def get_games(vod_id: str) -> Page[GameRow]:
        async with get_sessionmaker()() as s:
            if await s.get(Vod, vod_id) is None:
                raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
            return Page[GameRow](items=await _games(s, vod_id))

    @router.put("/vods/{vod_id}/games")
    async def put_games(request: Request, vod_id: str, body: GamesPut) -> Page[GameRow]:
        """Replace every games row: sorted by start, not overlapping, inside the VOD. Audited as
        ``vod.games.replace``, before and after."""
        async with get_sessionmaker()() as s:
            vod = await _locked_vod(s, vod_id)
            if vod.merged_into is not None:
                raise ApiError(
                    409,
                    "vod_merged",
                    f"{vod.id} was merged into {vod.merged_into.get('id')}; its games rows are that "
                    "VOD's now. Undo the merge first",
                )
            sent = [g.model_dump(exclude=_GAME_READ_ONLY, exclude_unset=True) for g in body.games]
            try:
                rows = vod_edits.games(sent, hhmmss_to_seconds(vod.duration))
            except ValueError as exc:
                raise ApiError(422, "invalid_games", str(exc)) from None
            before = await _games(s, vod.id)
            await s.execute(delete(Game).where(Game.vod_id == vod.id))
            if rows:
                await s.execute(insert(Game), [{"vod_id": vod.id, **r} for r in rows])
            after = await _games(s, vod.id)
            await record(
                s,
                AuditEntry(
                    "vod.games.replace",
                    _actor(request),
                    f"vod:{vod.id}",
                    before={"games": _games_audited(before)},
                    after={"games": _games_audited(after)},
                    request_id=request_id(request),
                ),
                table=audit.TABLE,
            )
            await s.commit()
        return Page[GameRow](items=after)

    @router.put("/vods/{vod_id}/chapters")
    async def put_chapters(request: Request, vod_id: str, body: ChaptersPut) -> VodDetail:
        """Replace every chapter: sorted by start, not overlapping, each longer than 0 and inside the VOD.
        Audited as ``vod.chapters.replace``, before and after."""
        async with get_sessionmaker()() as s:
            vod = await _locked_vod(s, vod_id)
            try:
                chapters = vod_edits.chapters(body.chapters, hhmmss_to_seconds(vod.duration))
            except ValueError as exc:
                raise ApiError(422, "invalid_chapters", str(exc)) from None
            before = {"chapters": vod.chapters or [], "chapters_locked": vod.chapters_locked}
            # A database trigger tells archive-api to drop its cached copies.
            await s.execute(update(Vod).where(Vod.id == vod.id).values(chapters=chapters, chapters_locked=body.locked))
            await s.refresh(vod)
            await record(
                s,
                AuditEntry(
                    "vod.chapters.replace",
                    _actor(request),
                    f"vod:{vod.id}",
                    before=before,
                    after={"chapters": vod.chapters or [], "chapters_locked": vod.chapters_locked},
                    request_id=request_id(request),
                ),
                table=audit.TABLE,
            )
            await s.commit()
        return await _detail(vod)

    async def replace_list(
        request: Request, vod_id: str, column: str, parse: Callable[[Vod], list[Any]], *, uploads: bool = False
    ) -> VodDetail:
        """Replace one of the VOD's lists (``parse`` validates the body against the VOD), audited as
        ``vod.<column>.replace`` before and after. ``uploads``: refused on a merged or synthetic VOD,
        which have no uploads of their own."""
        async with get_sessionmaker()() as s:
            vod = await _locked_vod(s, vod_id)
            if uploads and vod.merged_into is not None:
                raise ApiError(
                    409,
                    "vod_merged",
                    f"{vod.id} was merged into {vod.merged_into.get('id')}; its uploads are that VOD's now. "
                    "Undo the merge first",
                )
            if uploads and vod.synthetic is not None:
                raise ApiError(409, "vod_synthetic", f"{vod.id} is synthetic: it has no uploads of its own to edit")
            try:
                value = parse(vod)
            except ValueError as exc:
                raise ApiError(422, f"invalid_{column}", str(exc)) from None
            before = getattr(vod, column) or []
            # A database trigger tells archive-api to drop its cached copies.
            await s.execute(update(Vod).where(Vod.id == vod.id).values({column: value}))
            await s.refresh(vod)
            await record(
                s,
                AuditEntry(
                    f"vod.{column}.replace",
                    _actor(request),
                    f"vod:{vod.id}",
                    before={column: before},
                    after={column: getattr(vod, column) or []},
                    request_id=request_id(request),
                ),
                table=audit.TABLE,
            )
            await s.commit()
        return await _detail(vod)

    @router.put("/vods/{vod_id}/youtube")
    async def put_youtube(request: Request, vod_id: str, body: YoutubePut) -> VodDetail:
        return await replace_list(request, vod_id, "youtube", lambda vod: vod_edits.youtube(body.youtube, vod.youtube))

    @router.put("/vods/{vod_id}/drive")
    async def put_drive(request: Request, vod_id: str, body: DrivePut) -> VodDetail:
        return await replace_list(request, vod_id, "drive", lambda vod: vod_edits.drive(body.drive))

    @router.put("/vods/{vod_id}/censor")
    async def put_censor(request: Request, vod_id: str, body: CensorPut) -> VodDetail:
        return await replace_list(
            request,
            vod_id,
            "censor",
            lambda vod: vod_edits.censor(body.censor, hhmmss_to_seconds(vod.duration)),
            uploads=True,
        )

    # ── Synthetic VODs ──

    @router.post("/synthetic", status_code=201)
    async def create_synthetic(request: Request, body: SyntheticCreate) -> SyntheticView:
        """A synthetic VOD of ``segments``: windows ``[start, end)`` of real VODs placed at ``at`` (a
        missing ``at`` follows the segment before). ``supersedes`` (older dashboards): hide the sources
        and tag it ``vods``, as a merge does. With ``auto``
        (a playthrough that keeps itself up to date) the segments are every window of that game, kept so
        by the monitor, and ``segments`` is not needed. Audited as ``synthetic.create``."""
        view = await _synthetic(  # type: ignore[no-untyped-call]
            synthetic.create,
            body.id,
            [] if body.auto else _segments(body.segments),
            title=body.title,
            supersedes=body.supersedes,
            tags=body.tags,
            auto=_auto(body.auto),
        )
        await _audit(request, "synthetic.create", body.id, after=view)
        return SyntheticView(**view)

    @router.get("/synthetic/{vod_id}")
    async def get_synthetic(vod_id: str) -> SyntheticView:
        return SyntheticView(**await _synthetic(synthetic.get, vod_id))  # type: ignore[no-untyped-call]

    @router.get("/synthetic/{vod_id}/dmca")
    async def synthetic_dmca(vod_id: str) -> SyntheticDmca:
        """Manage shows these before a compile, which then needs ``accept_dmca`` (steps/compile) when
        there is any claim, overlapping or not."""
        await _synthetic(synthetic.get, vod_id)  # type: ignore[no-untyped-call]  # its 404
        return SyntheticDmca(**await synthetic.dmca_claims(vod_id))

    @router.put("/synthetic/{vod_id}")
    async def change_synthetic(request: Request, vod_id: str, body: SyntheticChange) -> SyntheticView:
        """Only the fields sent change; ``auto`` follows a game (its windows replace the segments) or,
        null, stops. Segments sent without ``auto`` are a hand edit, which stops it too. Audited as
        ``synthetic.update``, before and after."""
        if "auto" in body.model_fields_set:
            auto: dict[str, Any] | str | None = _auto(body.auto)
        else:
            auto = synthetic.KEEP if body.segments is None else None
        before, after = await _synthetic(  # type: ignore[no-untyped-call]
            synthetic.change,
            vod_id,
            segments=None if body.segments is None or body.auto else _segments(body.segments),
            title=body.title,
            supersedes=body.supersedes,
            tags=body.tags,
            auto=auto,
        )
        before, after = _snake(before), _snake(after)
        await _audit(request, "synthetic.update", vod_id, before=before, after=after)
        return SyntheticView(**after)

    @router.delete("/synthetic/{vod_id}")
    async def delete_synthetic(request: Request, vod_id: str) -> SyntheticView:
        """The synthetic VOD goes; the VODs it was made of are listed (and play) as before. Audited
        as ``synthetic.delete`` with what it was."""
        view = await _synthetic(synthetic.remove, vod_id)  # type: ignore[no-untyped-call]
        await _audit(request, "synthetic.delete", vod_id, before=view)
        return SyntheticView(**view)

    @router.get("/vods/{vod_id}/merge-candidates")
    async def merge_candidates(
        vod_id: str, hours: Annotated[int | None, Query(ge=1, le=168)] = None
    ) -> Page[MergeCandidate]:
        """What to offer to merge this VOD with (``POST /vods/{vod_id}/merge``): VODs that started up to
        ARCHIVE_MERGE_CANDIDATE_MINUTES after it ended and aren't merged into another; with ``hours``,
        those starting less than that after it ends or ending less than that before it starts."""
        found = await synthetic.merge_candidates(vod_id, settings.merge_candidate_minutes, hours)
        if found is None:
            raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
        a, rows = found

        def gap(v: Vod) -> float:
            first, then = (a, v) if v.created_at >= a.created_at else (v, a)
            return round((then.created_at - first.created_at).total_seconds()) - hhmmss_to_seconds(first.duration)

        return Page[MergeCandidate](
            items=[
                MergeCandidate(
                    id=v.id,
                    stream_id=v.stream_id,
                    title=v.title,
                    created_at=v.created_at,
                    duration=v.duration,
                    gap=gap(v),
                    titles_match=synthetic.normal_title(v.title) == synthetic.normal_title(a.title),
                )
                for v in rows
            ]
        )

    @router.post("/vods/{vod_id}/merge", status_code=201)
    async def merge_vods(request: Request, vod_id: str, body: MergeBody) -> SyntheticView:
        """This VOD and ``sources``, in the order they started, as one synthetic VOD tagged ``vods`` (id:
        ``id``, default ``{first}-m``); each placed where it started, or ``gaps[i]`` seconds after the one
        before ends. The old ``{source, gap?}`` makes ``{vod_id}+{source}``. The VODs are hidden: their
        links lead to it, and deleting it shows them again (ADR 0005)."""
        if body.sources is None and body.source is None:
            raise ApiError(422, "invalid_synthetic", "send sources (the VODs to merge with this one)")
        ids = [vod_id, *(body.sources if body.sources is not None else [body.source or ""])]
        async with get_sessionmaker()() as s:
            vods = {v.id: v for v in (await s.execute(select(Vod).where(Vod.id.in_(ids)))).scalars()}
        found = [synthetic.source_of(_real(vods, i)) for i in ids]
        if body.sources is None:
            segments = await _synthetic(compose.merge_segments, *found, body.gap)  # type: ignore[no-untyped-call]
            new_id = compose.merge_id(*ids)
        else:
            gaps = body.gaps if body.gaps is not None else [None] * (len(found) - 1)
            segments = await _synthetic(compose.merge_all, found, gaps)  # type: ignore[no-untyped-call]
            new_id = body.id or compose.merge_all_id(segments[0].source_id)
        view = await _synthetic(synthetic.create, new_id, segments, supersedes=True)  # type: ignore[no-untyped-call]
        merged = [x.source_id for x in segments]
        await _audit(request, "synthetic.create", view["id"], after=view, detail={"merge": merged})
        return SyntheticView(**view)

    @router.post("/vods/{vod_id}/split", status_code=201)
    async def split_vod(request: Request, vod_id: str, body: SplitBody) -> list[SyntheticView]:
        """Two synthetic VODs tagged ``vods``, ``ids`` (default ``{vod_id}-1`` and ``{vod_id}-2``), before and
        from ``at`` seconds (anywhere, mid-upload included); the VOD is hidden, its links lead to them."""
        async with get_sessionmaker()() as s:
            found = await s.get(Vod, vod_id)
        vod = _real({vod_id: found} if found else {}, vod_id)
        halves = await _synthetic(compose.split_segments, synthetic.source_of(vod), body.at)  # type: ignore[no-untyped-call]
        made: list[dict[str, Any]] = []
        try:
            for new_id, segments in zip(body.ids or compose.split_ids(vod.id), halves, strict=True):
                made.append(await _synthetic(synthetic.create, new_id, segments, supersedes=True))  # type: ignore[no-untyped-call]
        except ApiError:
            for view in made:  # both or neither
                await synthetic.remove(view["id"])
            raise
        for view in made:
            await _audit(request, "synthetic.create", view["id"], after=view, detail={"split": vod.id, "at": body.at})
        return [SyntheticView(**view) for view in made]

    @router.get("/playthrough-candidates")
    async def playthrough_candidates(game_id: Annotated[str, Query(min_length=1)]) -> Page[PlaythroughWindow]:
        """Every window of a VOD an auto playthrough of ``game_id`` would play (``synthetic.game_windows``:
        its chapters of it, adjacent ones joined), oldest first: trim them, then POST them to /synthetic as a playthrough (``tags:
        ["playthroughs"]``); a window's ``segment`` is ready to send."""
        async with get_sessionmaker()() as s:
            windows = await synthetic.game_windows(s, game_id)
        items = [
            PlaythroughWindow(
                vod_id=vod.id,
                title=vod.title,
                created_at=vod.created_at,
                start=start,
                end=end,
                length=end - start,
                segment=_snake(synthetic.window_segment(vod, start, end)),
            )
            for vod, start, end in windows
        ]
        return Page[PlaythroughWindow](items=items)

    return router


class SegmentIn(ApiModel):
    vod_id: str
    start: float | None = None  # default 0
    end: float | None = None  # default: the source's end
    at: float | None = None  # default: right after the segment before
    label: str | None = None


class AutoIn(ApiModel):
    game_id: str = Field(min_length=1)


def _auto(auto: AutoIn | None) -> dict[str, Any] | None:
    """As ``synthetic.auto`` stores it."""
    return None if auto is None else {"gameId": auto.game_id}


class SyntheticCreate(ApiModel):
    id: str  # not only digits (those are Twitch's)
    title: str | None = None  # default: the first source's
    supersedes: bool = False
    tags: list[str] = Field(default_factory=list)
    segments: list[SegmentIn] = Field(default_factory=list)  # not needed with ``auto``
    auto: AutoIn | None = None


class SyntheticChange(ApiModel):
    title: str | None = None
    supersedes: bool | None = None
    tags: list[str] | None = None
    segments: list[SegmentIn] | None = None
    auto: AutoIn | None = None


class SyntheticView(ApiModel):
    id: str
    title: str | None = None
    supersedes: bool
    tags: list[str]
    hidden: bool
    duration: str | None = None
    created_at: UtcDatetime | None = None
    made_at: UtcDatetime | None = None
    changed_at: UtcDatetime | None = None
    auto: dict[str, Any] | None = None  # {"game_id"}: it follows that game
    segments: list[dict[str, Any]]
    compiled: dict[str, Any] | None = None  # {"outdated"} when it has compiled uploads (steps/compile)


class DmcaClaim(ApiModel):
    vod_id: str
    part: int | None = None  # None: the whole VOD's claims
    mute: list[tuple[float, float]]
    blackout: list[tuple[float, float]]
    placed: bool = True  # false: a live copy's claim, not put on the VOD's time (counted as overlapping)


class SyntheticDmca(ApiModel):
    overlapping: list[DmcaClaim]  # ranges in a window it plays: a compile copies them
    other: list[DmcaClaim]  # the rest of those sources' claims


class MergeBody(ApiModel):
    sources: list[str] | None = Field(default=None, min_length=1, max_length=20)  # merged with this one
    gaps: list[float | None] | None = None  # one per join, in start order; null: where it started
    id: str | None = None  # default: the first VOD's id + "-m"
    source: str | None = None  # older dashboards: one VOD, id "{vod_id}+{source}"
    gap: float | None = None


class SplitBody(ApiModel):
    at: float
    ids: tuple[str, str] | None = None  # default: "{vod_id}-1", "{vod_id}-2"


class PlaythroughWindow(ApiModel):
    vod_id: str
    title: str | None = None
    created_at: UtcDatetime
    start: float
    end: float
    length: float
    segment: dict[str, Any]


_SYNTHETIC_CODES = {404: "vod_not_found", 409: "synthetic_conflict", 422: "invalid_synthetic"}


def _segments(items: list[SegmentIn]) -> list[Segment]:
    raw = [
        {"vodId": i.vod_id, **i.model_dump(include={"start", "end", "at", "label"}, exclude_none=True)} for i in items
    ]
    try:
        return compose.parse(raw)
    except compose.ComposeError as exc:
        raise ApiError(422, "invalid_synthetic", str(exc)) from None


async def _synthetic(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    """``fn`` (a ``synthetic`` change, or a ``compose`` builder), its refusals as problem details;
    a view (a dict) comes back snake_case."""
    try:
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
    except compose.ComposeError as exc:
        raise ApiError(422, "invalid_synthetic", str(exc)) from None
    except synthetic.SyntheticError as exc:
        raise ApiError(
            exc.status, _SYNTHETIC_CODES.get(exc.status, "synthetic_refused"), exc.msg, **exc.extra
        ) from None
    return _snake(result) if isinstance(result, dict) else result


def _real(vods: dict[str, Vod], vod_id: str) -> Vod:
    vod = vods.get(vod_id)
    if vod is None:
        raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
    if vod.synthetic is not None:
        raise ApiError(409, "synthetic_conflict", f"{vod_id} is a synthetic VOD; change its segments instead")
    return vod


async def _audit(request: Request, action: str, vod_id: str, **kwargs: Any) -> None:
    await audit.write(AuditEntry(action, _actor(request), f"vod:{vod_id}", request_id=request_id(request), **kwargs))


# ── VOD and job actions: the ones that reach YouTube or queue jobs ─────────


class RearchiveBody(ApiModel):
    """``type`` vod or live; one ``part``, or all of them; a local ``path`` to upload from instead of the
    local copy or a new download."""

    type: Literal["vod", "live"] = "vod"
    part: int | None = Field(default=None, ge=1)
    path: str | None = None


class YoutubeDelete(ApiModel):
    """Ids from the VOD's ``youtube`` list."""

    videos: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


class CensorReview(ApiModel):
    """``upload`` resumes the job with whatever censor ranges are set by now; ``cut`` first marks the
    flagged games' chapters restricted (and locks the chapters) so ``split`` leaves them out."""

    decision: Literal["upload", "cut"]


class JobQueued(ApiModel):
    job_id: int
    message: str


class FailedDelete(ApiModel):
    id: str
    error: str


class UploadsDeleted(ApiModel):
    deleted: list[str]
    failed: list[FailedDelete]
    describe_jobs: list[int]  # a describe job per type that still has uploads, to drop the dead links
    vod: VodDetail


_ADMIN_CODES = {400: "invalid", 404: "vod_not_found", 409: "conflict", 500: "not_configured"}


@contextmanager
def _admin_refusals() -> Iterator[None]:
    """The /admin helpers' refusals (``AdminError``, the job service's errors) as problem details."""
    try:
        yield
    except AdminError as exc:
        raise ApiError(exc.status, _ADMIN_CODES.get(exc.status, "refused"), exc.msg) from None
    except jobs.JobNotFound as exc:
        raise ApiError(404, "job_not_found", str(exc)) from None
    except jobs.JobConflict as exc:
        raise ApiError(409, "job_conflict", str(exc)) from None
    except jobs.InvalidJob as exc:
        raise ApiError(422, "invalid_job", str(exc)) from None


def _refuse_no_uploads(vod: Vod) -> None:
    """A merged or synthetic VOD has no uploads of its own."""
    if vod.merged_into is not None:
        raise ApiError(
            409,
            "vod_merged",
            f"{vod.id} was merged into {vod.merged_into.get('id')}; its uploads are that VOD's now. "
            "Undo the merge first",
        )
    if vod.synthetic is not None:
        raise ApiError(409, "vod_synthetic", f"{vod.id} is synthetic: it has no uploads of its own")


async def _vod(vod_id: str) -> Vod:
    async with get_sessionmaker()() as s:
        vod = await s.get(Vod, vod_id)
    if vod is None:
        raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")
    return vod  # type: ignore[no-any-return]


def actions_router(ctx: AdminContext, auth: Auth) -> APIRouter:
    """The /admin VOD and job actions the dashboard still needed: emotes, re-archive, deleting uploads,
    answering a censor review. They reuse the /admin helpers (``ctx``), so they refuse the same things."""
    router = APIRouter(tags=["vods"], dependencies=[Depends(auth)])

    @router.get("/vods/{vod_id}/emotes")
    async def get_emotes(vod_id: str) -> dict[str, Any] | None:
        """The saved emotes row as GET /emotes renders it (its own keys snake_case), or null if none was."""
        await _vod(vod_id)
        async with get_sessionmaker()() as s:
            query = select(*EMOTES.columns()).where(EMOTES.table.c.vod_id == vod_id)
            row = (await s.execute(query)).mappings().first()
        return _snake(EMOTES.to_json(row), deep=False) if row else None

    @router.post("/vods/{vod_id}/rearchive", status_code=202)
    async def rearchive(request: Request, vod_id: str, body: RearchiveBody) -> JobQueued:
        """Upload the VOD again with today's settings and censor ranges, deleting each upload it replaces
        once the new one is in place: a ``download`` job (``reupload`` for one part) with ``replace``."""
        vod = await _vod(vod_id)
        _refuse_no_uploads(vod)
        raw = body.model_dump(exclude_none=True)
        with _admin_refusals():
            payload = {**ctx.source_payload(vod, raw), "replace": True}
            if body.part is not None:
                ctx.single_part(payload, raw)
                job = await ctx.enqueue("reupload", vod.id, payload)
                message = f"Re-archiving {vod.id} part {body.part}; the old upload goes once the new one is up"
            else:
                job = await ctx.enqueue("download", vod.id, payload)
                message = f"Re-archiving {vod.id}; the old uploads go once the new ones are up"
        await _audit(request, "vod.rearchive", vod.id, detail={"job_id": job.id, **raw})
        return JobQueued(job_id=job.id, message=message)

    @router.post("/vods/{vod_id}/youtube/delete")
    async def delete_uploads(request: Request, vod_id: str, body: YoutubeDelete) -> UploadsDeleted:
        """Delete ``videos`` from YouTube (50 quota units each) and take them off the VOD's list; one
        already gone from YouTube is just taken off. Partial: ``failed`` are still on both."""
        vod = await _vod(vod_id)
        listed = {str(e.get("id")) for e in vod.youtube or []}
        unknown = [v for v in body.videos if v not in listed]
        if unknown:
            raise ApiError(422, "not_uploads", f"Not uploads of {vod.id}: {', '.join(unknown)}", videos=unknown)
        deleted: list[str] = []
        failed: list[FailedDelete] = []
        for video_id in dict.fromkeys(body.videos):
            try:
                await ctx.deps.youtube.delete(video_id)
            except youtube.YouTubeNotAuthorized as exc:
                raise ApiError(409, "youtube_not_authorized", str(exc)) from None
            except Exception as exc:
                log.warning("could not delete %s from YouTube: %s", video_id, exc)
                failed.append(FailedDelete(id=video_id, error=f"{type(exc).__name__}: {exc}"))
                continue
            deleted.append(video_id)
        dropped = await drop_youtube_entries(vod.id, set(deleted))
        after = await _vod(vod.id)
        described = []
        with _admin_refusals():
            for typ in sorted({e.get("type") or "vod" for e in dropped}):
                if any((e.get("type") or "vod") == typ for e in after.youtube or []):
                    job = await ctx.enqueue("describe", vod.id, ctx.type_payload(after, {"type": typ}))
                    described.append(job.id)
        if dropped:
            await _audit(
                request,
                "vod.youtube.delete",
                vod.id,
                before={"youtube": vod.youtube or []},
                after={"youtube": after.youtube or []},
                detail={"deleted": dropped, "failed": [f.model_dump() for f in failed]},
            )
        return UploadsDeleted(deleted=deleted, failed=failed, describe_jobs=described, vod=await _detail(after))

    @router.post("/jobs/{job_id}/censor-review")
    async def censor_review(request: Request, job_id: int, body: CensorReview) -> JobQueued:
        """Answer a job the ``censor_check`` step paused (``payload.censor_review``)."""
        with _admin_refusals():
            job = await jobs.get(job_id)
        review = (job.payload or {}).get("censor_review") or {}
        if job.state != "paused" or not review.get("games"):
            raise ApiError(409, "not_in_review", f"Job {job_id} is not paused for a censor review")
        cut: list[str] = []
        if body.decision == "cut":
            cut = sorted({str(g["gameId"]) for g in review["games"] if g.get("gameId")})
            if not cut:
                raise ApiError(
                    409, "nothing_to_cut", "IGDB could not be checked, so there are no games to cut; set censor ranges"
                )
            async with get_sessionmaker()() as s:
                vod = await _locked_vod(s, str(job.vod_id))
                # A database trigger tells archive-api to drop its cached copies.
                chapters = content_check.cut_games(vod.chapters, set(cut))
                await s.execute(update(Vod).where(Vod.id == vod.id).values(chapters=chapters, chapters_locked=True))
                await s.commit()
        with _admin_refusals():
            run = await ctx.service.resume(job_id, actor=_actor(request))
        message = f"Job {job_id} resumed at step {run.step} ({body.decision})"
        ctx.service.note(run, message)
        await audit.write(
            AuditEntry(
                "job.censor_review",
                _actor(request),
                f"job:{job_id}",
                detail={"decision": body.decision, "vod": str(job.vod_id), "cut": cut},
                request_id=request_id(request),
            )
        )
        return JobQueued(job_id=job_id, message=message)

    return router
