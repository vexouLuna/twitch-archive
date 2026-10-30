"""The site's tags and options, under /admin and as their /api/v2 twins (ADR 0003, step 7). Runtime
settings and storage are /api/v2's (api_v2_routes.py)."""

from __future__ import annotations

import re
from typing import Annotated, Any

from archive_common import audit
from archive_common import site_options as site_options_common
from archive_common import site_tags as site_tags_common
from archive_common.db import get_sessionmaker
from fastapi import Body, FastAPI, Request
from vex_platform.api import ApiError, request_id
from vex_platform.audit import AuditEntry

from . import (
    site_options,
    site_tags,
    svg_clean,
)
from .admin_shared import AdminContext, AdminError
from .api_v2_routes import _camel, _changed_by, _snake_key, _wire

V2_TAG_KEYS = {_snake_key(f) for f in site_tags_common.FIELDS} | {"shape"}  # shape: read back, ignored
# The camelCase fields the validators name in their refusals; quoted words are values (a card's id), not fields.
V1_NAMES = re.compile(
    r"(?<!')\b("
    + "|".join(f for f in (*site_tags_common.FIELDS, "playDefault", "homeCards") if f != _snake_key(f))
    + r")\b(?!')"
)


def v2_text(msg: str) -> str:
    """A validator's refusal for /api/v2: the fields it names in snake_case (/admin keeps camelCase)."""
    return V1_NAMES.sub(lambda m: _snake_key(m[0]), msg)


def register(app: FastAPI, ctx: AdminContext) -> None:
    auth = ctx.auth

    # ── Runtime settings ──────────────────────────────────────────────────

    def changed_by(request: Request) -> str:
        return getattr(request.state, "actor_login", None) or request.state.actor  # type: ignore[no-any-return]

    # ── Site tags (how vods.vexoul.net shows each VOD tag; archive-api serves them) ──

    async def site_tags_view() -> dict[str, Any]:
        async with get_sessionmaker()() as s:
            return site_tags.view(await site_tags_common.load(s))

    async def site_tags_change(pending) -> tuple[dict[str, Any], dict[str, Any]]:  # type: ignore[no-untyped-def]
        """The audit detail (before and after; never the SVG) and the GET body."""
        try:
            before, after, view = await pending
        except site_tags.SiteTagError as exc:
            raise AdminError(
                exc.status, exc.msg, extra={"tag": exc.tag, "field": exc.field} if exc.status == 400 else None
            ) from None
        return {"before": before, "after": after}, view

    @app.get("/admin/site/tags", dependencies=auth)
    async def get_site_tags() -> dict[str, Any]:
        """The tags in display order, with when and by whom they were last saved."""
        return await site_tags_view()

    @app.put("/admin/site/tags", dependencies=auth)
    async def put_site_tags(request: Request, body: Any = Body(...)) -> dict[str, Any]:
        """``{"tags": [...]}``: the whole list, or nothing when a tag is refused. A tag no longer listed
        loses its shape; ``shape`` in the body is ignored (PUT .../{tag}/shape sets it)."""
        try:
            tags = site_tags.validate(body)
        except site_tags.SiteTagError as exc:
            raise AdminError(400, exc.msg, extra={"tag": exc.tag, "field": exc.field}) from None
        request.state.audit_target = "site:tags"
        request.state.audit_detail, view = await site_tags_change(site_tags.save(tags, changed_by(request)))
        return view

    @app.put("/admin/site/tags/{tag}/shape", dependencies=auth)
    async def put_site_tag_shape(tag: str, request: Request) -> dict[str, Any]:
        """The tag's shape: the raw SVG (``content-type: image/svg+xml``), cleaned (svg_clean.py)."""
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "image/svg+xml":
            raise AdminError(415, "Send the SVG as content-type: image/svg+xml")
        raw = await request.body()
        if len(raw) > site_tags_common.SHAPE_MAX_BYTES:
            raise AdminError(413, f"The SVG is over {site_tags_common.SHAPE_MAX_BYTES // 1024} KB")
        try:
            svg = svg_clean.clean(raw)
        except svg_clean.UnsafeSvg as exc:
            raise AdminError(400, f"Refused SVG: {exc}") from None
        request.state.audit_target = f"site-tag:{tag}"
        request.state.audit_detail, view = await site_tags_change(site_tags.set_shape(tag, svg))
        return view

    @app.delete("/admin/site/tags/{tag}/shape", dependencies=auth)
    async def delete_site_tag_shape(tag: str, request: Request) -> dict[str, Any]:
        """No shape: the site draws the tag's label only."""
        request.state.audit_target = f"site-tag:{tag}"
        request.state.audit_detail, view = await site_tags_change(site_tags.set_shape(tag, None))
        return view

    # ── Site options (the homepage's cards; archive-api serves them) ──

    @app.get("/admin/site/options", dependencies=auth)
    async def get_site_options() -> dict[str, Any]:
        """``{homeCards, cards, playDefault, updatedAt, updatedBy}`` (``homeCards``: ``cards`` in the old shape);
        an option never saved reads as its default."""
        async with get_sessionmaker()() as s:
            return site_options.view(await site_options_common.load(s))

    @app.put("/admin/site/options", dependencies=auth)
    async def put_site_options(request: Request, body: Any = Body(...)) -> dict[str, Any]:
        """Any of the options; the others keep their value. All or nothing."""
        try:
            before, after, view = await site_options.save(body, changed_by(request))
        except site_options.SiteOptionError as exc:
            raise AdminError(400, exc.msg) from None
        request.state.audit_target = "site:options"
        request.state.audit_detail = {"before": before, "after": after}
        return view

    # ── /api/v2 twins: snake_case, problem details, and each change audits itself ──

    v2 = ctx.v2

    def tag_refusal(exc: site_tags.SiteTagError) -> ApiError:
        if exc.status == 404:
            return ApiError(404, "site_tag_not_found", exc.msg)
        field = {"field": _snake_key(exc.field)} if exc.field else {}
        return ApiError(422, "invalid_site_tag", v2_text(exc.msg), tag=exc.tag, **field)

    def unknown_keys(where: list[str | int], given: Any, known: set[str]) -> list[dict[str, Any]]:
        """FastAPI's 422 entries for keys a body may not have (camelCase /admin keys among them)."""
        if not isinstance(given, dict):
            return []
        return [
            {"loc": ["body", *where, k], "msg": "Extra inputs are not permitted", "type": "extra_forbidden"}
            for k in given
            if k not in known
        ]

    def refuse_unknown(errors: list[dict[str, Any]]) -> None:
        if errors:
            raise ApiError(422, "invalid", "The request did not validate.", errors=errors)

    async def v2_change(request: Request, action: str, target: str, pending: Any) -> dict[str, Any]:
        """A site tags change, audited once it committed (the change has its own transaction)."""
        try:
            before, after, view = await pending
        except site_tags.SiteTagError as exc:
            raise tag_refusal(exc) from None
        actor = request.state.actor
        await audit.write(AuditEntry(action, actor, target, before=before, after=after, request_id=request_id(request)))
        return _wire(view)  # type: ignore[no-any-return]  # _wire of a dict is a dict

    def v2_changed_by(request: Request) -> str:
        return _changed_by(request.state.actor)

    @v2.get("/site/tags", tags=["site"])
    async def v2_get_site_tags() -> dict[str, Any]:
        """``{tags, updated_at, updated_by}``: the tags in display order."""
        return _wire(await site_tags_view())  # type: ignore[no-any-return]  # _wire of a dict is a dict

    @v2.put("/site/tags", tags=["site"])
    async def v2_put_site_tags(request: Request, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """``{tags: [...]}``: the whole list, or nothing when a tag is refused (422 ``invalid_site_tag``,
        naming its ``tag`` and ``field``; 422 ``invalid`` for a key a tag doesn't have). Audited as
        ``site.tags.replace``."""
        errors = unknown_keys([], body, {"tags"})
        if isinstance(body.get("tags"), list):
            for i, item in enumerate(body["tags"]):
                errors += unknown_keys(["tags", i], item, V2_TAG_KEYS)
        refuse_unknown(errors)
        try:
            tags = site_tags.validate(_camel(body))
        except site_tags.SiteTagError as exc:
            raise tag_refusal(exc) from None
        return await v2_change(request, "site.tags.replace", "site:tags", site_tags.save(tags, v2_changed_by(request)))

    @v2.put("/site/tags/{tag}/shape", tags=["site"])
    async def v2_put_site_tag_shape(tag: str, request: Request) -> dict[str, Any]:
        """The raw SVG as ``content-type: image/svg+xml``, cleaned. Audited as ``site.tag.shape.set``."""
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "image/svg+xml":
            raise ApiError(415, "unsupported_media_type", "Send the SVG as content-type: image/svg+xml")
        raw = await request.body()
        if len(raw) > site_tags_common.SHAPE_MAX_BYTES:
            raise ApiError(413, "too_large", f"The SVG is over {site_tags_common.SHAPE_MAX_BYTES // 1024} KB")
        try:
            svg = svg_clean.clean(raw)
        except svg_clean.UnsafeSvg as exc:
            raise ApiError(422, "unsafe_svg", f"Refused SVG: {exc}") from None
        return await v2_change(request, "site.tag.shape.set", f"site-tag:{tag}", site_tags.set_shape(tag, svg))

    @v2.delete("/site/tags/{tag}/shape", tags=["site"])
    async def v2_delete_site_tag_shape(tag: str, request: Request) -> dict[str, Any]:
        """No shape: the site draws the tag's label only. Audited as ``site.tag.shape.clear``."""
        return await v2_change(request, "site.tag.shape.clear", f"site-tag:{tag}", site_tags.set_shape(tag, None))

    @v2.get("/site/options", tags=["site"])
    async def v2_get_site_options() -> dict[str, Any]:
        """``{cards, play_default, updated_at, updated_by}``; an option never saved reads as its default."""
        async with get_sessionmaker()() as s:
            view = site_options.view(await site_options_common.load(s))
        view.pop("homeCards", None)  # the old shape of ``cards``, for /admin's readers only
        return _wire(view)  # type: ignore[no-any-return]  # _wire of a dict is a dict

    @v2.put("/site/options", tags=["site"])
    async def v2_put_site_options(request: Request, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Any of ``cards`` and ``play_default``; the others keep their value. All or nothing (422
        ``invalid_site_options``; 422 ``invalid`` for another key, /admin's ``home_cards`` among them).
        Audited as ``site.options.update``."""
        refuse_unknown(unknown_keys([], body, {"cards", "play_default"}))
        try:
            before, after, view = await site_options.save(_camel(body), v2_changed_by(request))
        except site_options.SiteOptionError as exc:
            raise ApiError(422, "invalid_site_options", v2_text(exc.msg)) from None
        await audit.write(
            AuditEntry(
                "site.options.update",
                request.state.actor,
                "site:options",
                before=before,
                after=after,
                request_id=request_id(request),
            )
        )
        view.pop("homeCards", None)
        return _wire(view)  # type: ignore[no-any-return]  # _wire of a dict is a dict
