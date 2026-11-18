"""/api/v2/thumbnail: the YouTube thumbnail template (thumbnails.py) and making thumbnails again.

    GET  /api/v2/thumbnail/template      {template, saved, fields}: the template in use (the default until one is
                                         saved) and the fields its text can use
    PUT  /api/v2/thumbnail/template      {template}: check and save it (422 invalid_template says what is wrong)
    POST /api/v2/thumbnail/preview       {template, vod_id?}: the template drawn as a 1280x720 JPEG, over a frame of
                                         that VOD's file on this worker (or a plain frame), with sample fields
    POST /api/v2/vods/{vod_id}/thumbnails  {type, parts?}: 202 {job_id}, a ``thumbnail`` job setting the template's
                                         picture on the VOD's uploads of that type (only ``parts``, if given)

Saving the template is audited (``thumbnail.template``), as is queuing a job (``vod.thumbnails``).
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any, Literal

from archive_common import audit
from archive_common.config import Settings
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import Field
from vex_platform.api import ApiError, ApiModel, request_id
from vex_platform.audit import AuditEntry

from . import ffmpeg, planning, thumbnails
from .admin_shared import AdminContext
from .api_v2_routes import Auth, JobQueued, _actor, _admin_refusals, _audit, _vod

# What a preview fills the fields with, where the VOD (if one is given) has nothing better.
SAMPLE = {"part": 2, "total": 3, "games": "Elden Ring · Celeste", "label": "VOD", "title": "Stream title"}


class Template(ApiModel):
    at: float
    lines: list[dict[str, Any]]


class TemplateState(ApiModel):
    template: Template
    saved: bool  # false: no template saved yet, so this is the default
    fields: list[str]


class TemplateBody(ApiModel):
    template: dict[str, Any]


class PreviewBody(ApiModel):
    template: dict[str, Any]
    vod_id: str | None = None


class ThumbnailsAgain(ApiModel):
    type: Literal["vod", "live", "compiled"] = "vod"
    parts: list[int] | None = Field(default=None, min_length=1)


def _checked(raw: Any) -> dict[str, Any]:
    try:
        return thumbnails.check(raw)
    except thumbnails.TemplateError as exc:
        raise ApiError(422, "invalid_template", str(exc)) from None


def _frame_source(settings: Settings, vod_id: str) -> Path | None:
    """The VOD's own file on this worker, or its first part, while it is still there."""
    folder = settings.vod_dir / vod_id
    own = folder / f"{vod_id}.mp4"
    if own.is_file():
        return own
    return next(iter(sorted(folder.glob("*.mp4"))), None) if folder.is_dir() else None


def thumbnails_router(ctx: AdminContext, auth: Auth) -> APIRouter:
    router = APIRouter(tags=["thumbnails"], dependencies=[Depends(auth)])
    settings = ctx.settings

    @router.get("/thumbnail/template")
    async def get_template() -> TemplateState:
        return TemplateState(
            template=Template(**await thumbnails.load()), saved=await thumbnails.saved(), fields=list(thumbnails.FIELDS)
        )

    @router.put("/thumbnail/template")
    async def put_template(request: Request, body: TemplateBody) -> TemplateState:
        template = _checked(body.template)
        before = await thumbnails.load()
        await thumbnails.save(template)
        await audit.write(
            AuditEntry(
                "thumbnail.template",
                _actor(request),
                "setting:thumbnail_template",
                before=before,
                after=template,
                request_id=request_id(request),
            )
        )
        return TemplateState(template=Template(**template), saved=True, fields=list(thumbnails.FIELDS))

    @router.post("/thumbnail/preview", response_class=Response)
    async def preview(body: PreviewBody) -> Response:
        """``image/jpeg``. The frame is taken ``at`` into the VOD's whole file, not one part's."""
        template = _checked(body.template)
        values: dict[str, Any] = {**SAMPLE, "channel": settings.channel, "date": "2026-01-31"}
        src = None
        if body.vod_id:
            vod = await _vod(body.vod_id)
            values.update(title=(vod.title or "").strip(), date=planning.local_date(vod.created_at, settings.timezone))
            if vod.platform == "upload":
                values["label"] = thumbnails.label_of("custom_upload", "vod")
            src = _frame_source(settings, vod.id)
        seconds = 0.0
        if src is not None:
            seconds = await ffmpeg.probe_duration(src)
        work = settings.data_dir / "thumbnail-preview" / secrets.token_hex(6)
        try:
            out = await thumbnails.render(src, template["at"] * seconds, template, values, work / "preview.jpg")
            return Response(out.read_bytes(), media_type="image/jpeg", headers={"cache-control": "no-store"})
        except Exception as exc:
            raise ApiError(500, "preview_failed", f"could not draw the preview: {exc}") from None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @router.post("/vods/{vod_id}/thumbnails", status_code=202)
    async def thumbnails_again(request: Request, vod_id: str, body: ThumbnailsAgain) -> JobQueued:
        """The template's thumbnail again on each upload of ``type`` (or only ``parts``), whether or not
        ``youtube_thumbnail`` is on; a re-upload gets its own from its job already."""
        vod = await _vod(vod_id)
        listed = sorted({int(e.get("part") or 1) for e in vod.youtube or [] if e.get("type") == body.type})
        if not listed:
            raise ApiError(409, "no_uploads", f"{vod.id} has no {body.type} uploads")
        missing = sorted(set(body.parts or []) - set(listed))
        if missing:
            raise ApiError(422, "not_uploads", f"no {body.type} upload of part {', '.join(map(str, missing))}")
        payload: dict[str, Any] = {"type": body.type}
        if body.parts:
            payload["only_parts"] = sorted(set(body.parts))
        with _admin_refusals():
            # type_payload knows only vod/live (and keeps only its own keys): a compiled upload needs no stream_id.
            base = {"type": "compiled", "manual": True} if body.type == "compiled" else ctx.type_payload(vod, payload)
            job = await ctx.enqueue("thumbnail", vod.id, {**base, **payload})
        await _audit(request, "vod.thumbnails", vod.id, after={**payload, "job_id": job.id})
        parts = f"part {', '.join(map(str, body.parts))}" if body.parts else "every part"
        return JobQueued(job_id=job.id, message=f"Making the {body.type} thumbnails of {vod.id} again ({parts})")

    return router
