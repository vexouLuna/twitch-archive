"""The site's tags and options. Runtime settings and storage are /api/v2's (api_v2_routes.py)."""

from __future__ import annotations

from typing import Any

from archive_common import site_options as site_options_common
from archive_common import site_tags as site_tags_common
from archive_common.db import get_sessionmaker
from fastapi import Body, FastAPI, Request

from . import (
    site_options,
    site_tags,
    svg_clean,
)
from .admin_shared import AdminContext, AdminError


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
        """``{homeCards, playDefault, updatedAt, updatedBy}``; an option never saved reads as its default."""
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
