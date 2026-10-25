"""/api/v2's server files for the dashboard's VOD preview, one viewing tab at a time (media_lease.py).

    GET    /api/v2/vods/{vod_id}/media            the VOD's files still on the server, and who holds the lease
    GET    /api/v2/vods/{vod_id}/media/{name}?lease=  one of them, with Range (206), while ``lease`` holds it
    POST   /api/v2/media/lease                    {lease_id, vod_id, file, take_over}: take the lease
                                                  (409 media_in_use with the holder; audited only on a take-over)
    PUT    /api/v2/media/lease/{lease_id}         heartbeat (410 lease_lost with who has it now)
    DELETE /api/v2/media/lease/{lease_id}         release it

A file is named, never given as a path: ``source`` is the archive job's MP4 (``<id>.mp4``), ``covered`` and
``censored`` what the censor step made of it (in ``censor/``). They are gone once the job's cleanup ran,
unless ``keep_mp4`` (the source) or ``keep_files_days`` (all, for that long).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO

from archive_common import audit
from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from vex_platform.actor import Actor
from vex_platform.api import ApiError, ApiModel, UtcDatetime, request_id
from vex_platform.audit import AuditEntry
from vex_platform.audit.sqlalchemy import record

from . import media_lease
from .media_lease import Lease

Auth = Callable[[Request], Any]
CHUNK = 256 * 1024
RECHECK_S = 2.0  # a stream re-checks its lease at least this often
_VOD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class MediaName(StrEnum):
    source = "source"
    covered = "covered"
    censored = "censored"


LABELS = {
    MediaName.source: "Source (uncensored)",
    MediaName.covered: "Censored (covers only)",
    MediaName.censored: "Censored",
}


def media_path(vod_dir: Path, vod_id: str, name: MediaName) -> Path | None:
    """The file behind ``name`` for ``vod_id`` if it is a regular file inside ``vod_dir`` (no symlink, no escape)."""
    if not _VOD_ID.match(vod_id):
        return None
    rel = {
        MediaName.source: Path(vod_id) / f"{vod_id}.mp4",
        MediaName.covered: Path(vod_id) / "censor" / f"{vod_id}-covered.mp4",
        MediaName.censored: Path(vod_id) / "censor" / f"{vod_id}-censored.mp4",
    }[name]
    path = vod_dir / rel
    try:
        base = vod_dir.resolve(strict=True)
        if any(p.is_symlink() for p in (path, path.parent, vod_dir / vod_id)):
            return None
        if not path.resolve(strict=True).is_relative_to(base) or not path.is_file():
            return None
    except OSError:
        return None
    return path


class Holder(ApiModel):
    login: str | None  # the Twitch login, None for the password login or the API key
    kind: str
    browser: str
    vod_id: str | None = None
    file: str | None = None
    acquired_at: UtcDatetime
    heartbeat_at: UtcDatetime
    yours: bool = False  # held by the tab that asked (``lease``)


def _holder(lease: Lease, mine: str | None = None) -> Holder:
    h = lease.holder
    return Holder(
        login=h.get("login"),
        kind=h.get("kind", "password"),
        browser=h.get("browser", "A browser"),
        vod_id=h.get("vod_id"),
        file=h.get("file"),
        acquired_at=lease.acquired_at,
        heartbeat_at=lease.heartbeat_at,
        yours=mine is not None and lease.lease_id == mine,
    )


class MediaFile(ApiModel):
    name: MediaName
    label: str
    size: int
    modified_at: UtcDatetime


class MediaList(ApiModel):
    items: list[MediaFile]
    lease: Holder | None
    keep_mp4: bool
    keep_files_days: float


class LeaseClaim(ApiModel):
    lease_id: str
    vod_id: str | None = None
    file: MediaName | None = None
    take_over: bool = False


def _lease_id(value: str) -> str:
    if not media_lease.LEASE_ID.match(value):
        raise ApiError(422, "invalid_lease", "lease_id must be 16-64 letters, digits, - or _")
    return value


def _in_use(lease: Lease | None) -> dict[str, Any]:
    return {"holder": _holder(lease).model_dump(mode="json") if lease else None}


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``(first, last)`` of a single ``bytes=`` range, None for the whole file; a bad one is a 416."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match or match.groups() == ("", ""):
        return None  # several ranges or another unit: the whole file, as RFC 9110 allows
    first, last = match.groups()
    if first == "":
        start, end = max(size - int(last), 0), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ApiError(416, "bad_range", f"the file is {size} bytes", headers={"Content-Range": f"bytes */{size}"})
    return start, end


async def _chunks(f: BinaryIO, start: int, end: int, lease_id: str) -> AsyncIterator[bytes]:
    """The bytes ``start``..``end``; stops (closing the response) once the lease is lost."""
    loop = asyncio.get_running_loop()
    checked = loop.time()
    pos = start
    try:
        await asyncio.to_thread(f.seek, start)
        while pos <= end:
            if loop.time() - checked >= RECHECK_S:
                if not await media_lease.holds(lease_id):
                    return
                checked = loop.time()
            data = await asyncio.to_thread(f.read, min(CHUNK, end - pos + 1))
            if not data:
                return
            pos += len(data)
            yield data
    finally:
        await asyncio.to_thread(f.close)


def media_router(auth: Auth, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["media"], dependencies=[Depends(auth)])

    async def require_vod(vod_id: str) -> None:
        async with get_sessionmaker()() as s:
            if await s.get(Vod, vod_id) is None:
                raise ApiError(404, "vod_not_found", f"no VOD {vod_id}")

    @router.get("/vods/{vod_id}/media")
    async def list_media(vod_id: str, lease: str | None = None) -> MediaList:
        """The files the preview can stream, and the lease's holder (``yours`` when it is ``lease``)."""
        await require_vod(vod_id)
        items = []
        for name in MediaName:
            path = await asyncio.to_thread(media_path, settings.vod_dir, vod_id, name)
            if path is not None:
                st = await asyncio.to_thread(path.stat)
                at = dt.datetime.fromtimestamp(st.st_mtime, dt.UTC)
                items.append(MediaFile(name=name, label=LABELS[name], size=st.st_size, modified_at=at))
        held = await media_lease.current()
        return MediaList(
            items=items,
            lease=_holder(held, lease) if held else None,
            keep_mp4=settings.keep_mp4,
            keep_files_days=settings.keep_files_days,
        )

    @router.post("/media/lease")
    async def claim_lease(request: Request, body: LeaseClaim) -> Holder:
        """Take the lease for this tab: 409 ``media_in_use`` (with ``holder``) while another tab holds it,
        unless ``take_over``. A take-over of a held lease is audited as ``media.take_over``."""
        lease_id = _lease_id(body.lease_id)
        actor: Actor = request.state.actor
        holder = {
            "login": actor.login,
            "kind": actor.kind,
            "browser": media_lease.browser_of(request.headers.get("user-agent")),
            "vod_id": body.vod_id,
            "file": body.file.value if body.file else None,
        }
        mine, other = await media_lease.claim(lease_id, media_lease.owner_of(actor), holder, take_over=body.take_over)
        if mine is None:
            raise ApiError(409, "media_in_use", "Another tab is using the server's video files", **_in_use(other))
        if other is not None:
            async with get_sessionmaker()() as s, s.begin():
                await record(
                    s,
                    AuditEntry(
                        "media.take_over",
                        actor,
                        f"vod:{body.vod_id}" if body.vod_id else None,
                        before=other.holder | {"owner": other.owner},
                        after=holder,
                        request_id=request_id(request),
                    ),
                    table=audit.TABLE,
                )
        return _holder(mine, lease_id)

    @router.put("/media/lease/{lease_id}", status_code=204)
    async def renew_lease(lease_id: str) -> Response:
        """Every ``media_lease.HEARTBEAT`` seconds; 410 ``lease_lost`` (with who has it now) once another took it."""
        if not await media_lease.heartbeat(_lease_id(lease_id)):
            raise ApiError(
                410, "lease_lost", "Another tab took the server's video files", **_in_use(await media_lease.current())
            )
        return Response(status_code=204)

    @router.delete("/media/lease/{lease_id}", status_code=204)
    async def release_lease(lease_id: str) -> Response:
        await media_lease.release(_lease_id(lease_id))
        return Response(status_code=204)

    @router.get("/vods/{vod_id}/media/{name}", response_model=None)
    async def stream_media(request: Request, vod_id: str, name: MediaName, lease: str = "") -> StreamingResponse:
        """The file, whole (200) or one ``Range`` (206), while ``lease`` holds the lease (else 410 ``lease_lost``)."""
        if not media_lease.LEASE_ID.match(lease) or not await media_lease.holds(lease):
            raise ApiError(
                410,
                "lease_lost",
                "This tab doesn't hold the server's video files",
                **_in_use(await media_lease.current()),
            )
        path = await asyncio.to_thread(media_path, settings.vod_dir, vod_id, name)
        if path is None:
            raise ApiError(404, "media_not_found", f"no {name} file for VOD {vod_id} on the server")
        f = await asyncio.to_thread(path.open, "rb")
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
            span = _byte_range(request.headers.get("range"), size)
        except BaseException:
            f.close()
            raise
        start, end = span or (0, size - 1)
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
        if span:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            _chunks(f, start, end, lease), status_code=206 if span else 200, media_type="video/mp4", headers=headers
        )

    return router
