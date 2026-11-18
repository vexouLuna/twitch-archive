"""/api/v2/uploads: a video sent from Manage in chunks, then published as a custom upload (custom_upload.py).

    POST   /api/v2/uploads              {filename, size_bytes}: 201, the upload (its id, chunk_bytes)
    GET    /api/v2/uploads              {items}: the unfinished uploads, newest first
    GET    /api/v2/uploads/{id}         how far it is (received_bytes)
    PUT    /api/v2/uploads/{id}?offset= the next chunk as the raw body, at most chunk_bytes, at received_bytes
    DELETE /api/v2/uploads/{id}         cancel: the file is deleted
    POST   /api/v2/uploads/{id}/finish  {title, description, privacy}: 201 {vod_id, job_id}; the hidden VOD and
                                        its custom_upload job, which pauses for review before ``censor``

Starting, finishing and cancelling are audited (``upload.start``, ``upload.finish``, ``upload.cancel``); chunks
are not. Every new upload first deletes the abandoned ones (``custom_upload_abandon_hours``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Literal

from archive_common import audit
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_common.timeutil import format_hhmmss
from fastapi import APIRouter, Depends, Request
from pydantic import Field
from vex_platform.api import ApiError, ApiModel, Page, UtcDatetime, request_id
from vex_platform.audit import AuditEntry

from . import custom_upload
from .admin_shared import AdminContext
from .api_v2_routes import Auth, _actor, _admin_refusals, _changed_by
from .custom_upload import UploadError


class Upload(ApiModel):
    id: str
    filename: str
    size_bytes: int
    received_bytes: int
    chunk_bytes: int
    created_at: UtcDatetime
    updated_at: UtcDatetime
    created_by: str | None = None


class UploadStart(ApiModel):
    filename: str = Field(min_length=1, max_length=1000)
    size_bytes: int = Field(gt=0)


class UploadFinish(ApiModel):
    title: str
    description: str = ""
    privacy: Literal["private", "unlisted", "public"] = "private"


class UploadFinished(ApiModel):
    vod_id: str
    job_id: int
    duration_seconds: float


def _refused(exc: UploadError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.msg, **exc.extra)


def uploads_router(ctx: AdminContext, auth: Auth) -> APIRouter:
    router = APIRouter(tags=["uploads"], dependencies=[Depends(auth)])
    settings = ctx.settings  # the live object: the dashboard's changes to the two settings apply at once

    @router.post("/uploads", status_code=201)
    async def start_upload(request: Request, body: UploadStart) -> Upload:
        """Room for ``size_bytes`` (at most ``custom_upload_max_gb``; 507 ``disk_full`` without twice that free)."""
        await asyncio.to_thread(custom_upload.sweep, settings.data_dir, settings.custom_upload_abandon_hours)
        actor = _actor(request)
        try:
            up = custom_upload.start(
                settings.data_dir,
                body.filename,
                body.size_bytes,
                int(settings.custom_upload_max_gb * 2**30),
                _changed_by(actor),
            )
        except UploadError as exc:
            raise _refused(exc) from None
        await audit.write(
            AuditEntry("upload.start", actor, f"upload:{up['id']}", after=up, request_id=request_id(request))
        )
        return Upload(**up)

    @router.get("/uploads")
    async def list_uploads() -> Page[Upload]:
        return Page[Upload](items=[Upload(**u) for u in custom_upload.listing(settings.data_dir)])

    @router.get("/uploads/{upload_id}")
    async def get_upload(upload_id: str) -> Upload:
        try:
            return Upload(**custom_upload.status(settings.data_dir, upload_id))
        except UploadError as exc:
            raise _refused(exc) from None

    @router.put("/uploads/{upload_id}")
    async def put_chunk(request: Request, upload_id: str, offset: int) -> Upload:
        """The raw body is the bytes from ``offset``, which must equal ``received_bytes`` (409 ``offset_mismatch``
        says where to go on from). A chunk is all or nothing: one cut off is dropped, and sent again."""
        try:
            return Upload(**await custom_upload.write_chunk(settings.data_dir, upload_id, offset, request.stream()))
        except UploadError as exc:
            raise _refused(exc) from None

    @router.delete("/uploads/{upload_id}")
    async def cancel_upload(request: Request, upload_id: str) -> Upload:
        try:
            up = custom_upload.cancel(settings.data_dir, upload_id)
        except UploadError as exc:
            raise _refused(exc) from None
        await audit.write(
            AuditEntry(
                "upload.cancel", _actor(request), f"upload:{upload_id}", before=up, request_id=request_id(request)
            )
        )
        return Upload(**up)

    @router.post("/uploads/{upload_id}/finish", status_code=201)
    async def finish_upload(request: Request, upload_id: str, body: UploadFinish) -> UploadFinished:
        """Check the whole file with ffprobe (422 ``not_video``), make the hidden ``upload-…`` VOD with ``title``
        and queue its ``custom_upload`` job; ``description`` and ``privacy`` are what YouTube gets."""
        try:
            title, description, privacy = custom_upload.check_details(body.title, body.description, body.privacy)
            folder = settings.vod_dir / upload_id
            up, seconds = await custom_upload.take(settings.data_dir, upload_id, folder / f"{upload_id}.upload")
        except UploadError as exc:
            raise _refused(exc) from None
        async with get_sessionmaker()() as s:
            s.add(
                Vod(
                    id=upload_id,
                    title=title,
                    hidden=True,
                    platform="upload",
                    duration=format_hhmmss(seconds),
                    created_at=dt.datetime.now(dt.UTC),
                )
            )
            await s.commit()
        payload: dict[str, Any] = {
            "upload_file": str(folder / f"{upload_id}.upload"),
            "custom": {"description": description, "privacy": privacy, "filename": up["filename"]},
        }
        with _admin_refusals():
            job = await ctx.enqueue("custom_upload", upload_id, payload)
        await audit.write(
            AuditEntry(
                "upload.finish",
                _actor(request),
                f"vod:{upload_id}",
                after={"title": title, "privacy": privacy, "size_bytes": up["size_bytes"], "job_id": job.id},
                request_id=request_id(request),
            )
        )
        return UploadFinished(vod_id=upload_id, job_id=job.id, duration_seconds=seconds)

    return router
