"""/api/v2/songs: the censor song library (songs.py).

    GET    /api/v2/songs               {items}: every song, newest first
    POST   /api/v2/songs?name=         the raw audio file as the body (at most songs.MAX_BYTES): 201, the song
    GET    /api/v2/songs/{song_id}/audio  the stored file (audio/mp4), to listen to it
    DELETE /api/v2/songs/{song_id}     refused (409 song_in_use) while a VOD's censor range plays it

The upload is audited as ``song.add``, the delete as ``song.delete``.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from archive_common import audit
from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from vex_platform.api import ApiError, ApiModel, Page, UtcDatetime, request_id
from vex_platform.audit import AuditEntry

from . import songs
from .api_v2_routes import Auth, _actor, _changed_by


class Song(ApiModel):
    id: str
    name: str
    duration_seconds: float
    size_bytes: int
    created_at: UtcDatetime
    created_by: str | None = None


def _refused(exc: songs.SongError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.msg)


async def _receive(request: Request, folder: Path) -> Path:
    """The request body into a temporary file in ``folder``, refused (413) past ``songs.MAX_BYTES``."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > songs.MAX_BYTES:
        raise ApiError(413, "too_large", f"a song file is at most {songs.MAX_BYTES // (1024 * 1024)} MB")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".upload-{secrets.token_hex(8)}"
    size = 0
    try:
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > songs.MAX_BYTES:
                    raise ApiError(413, "too_large", f"a song file is at most {songs.MAX_BYTES // (1024 * 1024)} MB")
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise ApiError(422, "not_audio", "the body is empty: send the audio file as the body")
    return tmp


async def vods_playing(song_id: str) -> list[str]:
    """The VODs with a censor range that plays the song."""
    async with get_sessionmaker()() as s:
        rows = await s.execute(select(Vod.id).where(Vod.censor.contains([{"song": song_id}])).order_by(Vod.id))
    return [str(r) for r in rows.scalars()]


def songs_router(auth: Auth, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["songs"], dependencies=[Depends(auth)])
    data_dir = settings.data_dir

    @router.get("/songs")
    async def list_songs() -> Page[Song]:
        return Page[Song](items=[Song(**m) for m in songs.listing(data_dir)])

    @router.post("/songs", status_code=201)
    async def add_song(request: Request, name: str) -> Song:
        """The body is the audio file itself (any content type); ``name`` is what the editor lists it as.
        Taken only if ffprobe reads it as audio, then stored re-encoded as AAC (at most 10 minutes)."""
        actor = _actor(request)
        try:
            songs.clean_name(name)  # before the body is read
            upload = await _receive(request, songs.song_dir(data_dir))
            meta = await songs.add(data_dir, upload, name, _changed_by(actor))
        except songs.SongError as exc:
            raise _refused(exc) from None
        await audit.write(
            AuditEntry("song.add", actor, f"song:{meta['id']}", after=meta, request_id=request_id(request))
        )
        return Song(**meta)

    @router.get("/songs/{song_id}/audio", response_model=None)
    async def song_audio(song_id: str) -> FileResponse:
        file = songs.path(data_dir, song_id)
        if file is None:
            raise ApiError(404, "song_not_found", f"no song {song_id}")
        return FileResponse(file, media_type="audio/mp4")

    @router.delete("/songs/{song_id}")
    async def delete_song(request: Request, song_id: str) -> Song:
        """Refused while a censor range plays it: change those ranges first (the error lists the VODs)."""
        if songs.is_id(song_id) and (using := await vods_playing(song_id)):
            raise ApiError(
                409,
                "song_in_use",
                f"censor ranges of {', '.join(using)} play this song; give them another song first",
                vods=using,
            )
        try:
            meta: dict[str, Any] = songs.delete(data_dir, song_id)
        except songs.SongError as exc:
            raise _refused(exc) from None
        await audit.write(
            AuditEntry("song.delete", _actor(request), f"song:{song_id}", before=meta, request_id=request_id(request))
        )
        return Song(**meta)

    return router
