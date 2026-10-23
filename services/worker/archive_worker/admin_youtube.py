"""YouTube: the OAuth consent flow and its callback, token status and the uploads check."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from vex_platform.actor import Actor

from . import (
    youtube,
    youtube_check,
)
from .admin_shared import AdminContext, AdminError, _actor, _ok


def register(app: FastAPI, ctx: AdminContext) -> None:
    deps = ctx.deps
    settings = ctx.settings
    auth = ctx.auth

    # ── YouTube OAuth ─────────────────────────────────────────────────────

    # The consent flows this worker started, by their state: when, and who, so the callback (which has no
    # session) accepts only these and audits the connect as them. In memory: a flow started before a
    # restart has to be started again.
    connecting: dict[str, tuple[float, Actor]] = {}

    @app.get("/admin/youtube/auth", dependencies=auth)
    async def youtube_auth(redirect: bool = False):  # type: ignore[no-untyped-def]
        if not settings.google_client_id:
            raise AdminError(500, "google_client_id is not configured")
        state = youtube.new_state()
        now = time.monotonic()
        for key in [k for k, (at, _) in connecting.items() if now - at > youtube.STATE_TTL_S]:
            del connecting[key]
        connecting[state] = (now, _actor.get())
        url = youtube.consent_url(settings, state)
        if redirect:
            return RedirectResponse(url)
        return {"error": False, "url": url, **await youtube.token_status()}

    @app.get("/admin/youtube/status", dependencies=auth)
    async def youtube_status() -> dict[str, Any]:
        # A real refresh against Google, not just "is a token stored".
        return await deps.youtube.check()

    # Whether every upload is still on YouTube and whole (youtube_check.py); the timer runs it every
    # youtube_check_hours, and the dashboard can run it now.
    @app.get("/admin/youtube/videos", dependencies=auth)
    async def youtube_videos() -> dict[str, Any]:
        return {"running": deps.youtube_check.running, "last": await youtube_check.last()}

    @app.post("/admin/youtube/videos/check", dependencies=auth)
    async def youtube_videos_check() -> dict[str, Any]:
        if deps.youtube_check.running:
            raise AdminError(409, "A YouTube check is already running")
        return {"running": False, "last": await deps.youtube_check.run()}

    # With the slash too (the old app's redirect URI): FastAPI would answer it with a redirect built from
    # the Host header, which behind the site's proxy loses the /backend-admin prefix and lands on the site.
    @app.get("/admin/refreshtoken")
    @app.get("/admin/refreshtoken/", include_in_schema=False)
    async def refresh_token(code: str | None = None, state: str | None = None) -> dict[str, Any]:
        # Google redirects the browser here, so this cannot carry the API key or rely on the session
        # cookie. What proves the request is the state: one /admin/youtube/auth issued, unused, and
        # younger than STATE_TTL_S. It is consumed here, so a state works once.
        started = connecting.pop(state, None) if state else None
        if not code or started is None or time.monotonic() - started[0] > youtube.STATE_TTL_S:
            raise AdminError(403, "Invalid or expired OAuth state; start again at /admin/youtube/auth")
        actor = started[1]
        before = youtube.token_summary(await youtube.load_token())
        if before is not None and deps.youtube.last_check:
            before["channel"] = deps.youtube.last_check.get("channel")
        try:
            token = await youtube.exchange_code(settings, code)
        except Exception as exc:
            await youtube.audit_connect(actor, before, None, error=f"{type(exc).__name__}: {exc}")
            raise
        status = await deps.youtube.check()
        after = {**(youtube.token_summary(token) or {}), "channel": status.get("channel")}
        await youtube.audit_connect(actor, before, after, error=None if status["valid"] else status["error"])
        if not status["valid"]:
            raise AdminError(500, f"Token stored but not usable: {status['error']}")
        return _ok("YouTube authorized. You can close this tab.")
