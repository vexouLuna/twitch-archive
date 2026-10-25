"""Health and readiness, the dashboard's status (/admin/health), job kinds and the audit log."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from archive_common import http
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import Stream
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from . import (
    jobs,
    youtube_quota,
)
from .admin_shared import READY_TIMEOUT_S, YOUTUBE_CHECK_MAX_AGE, AdminContext, _audit_json, _job_counts, _job_json, log
from .events import iso_utc
from .job_rows import ALL_JOBS

HEARTBEAT_MAX_AGE_S = 60  # procrastinate beats every 10 s; older than this, no worker is running


async def _worker_beat(s) -> dt.datetime | None:  # type: ignore[no-untyped-def]
    """The newest job-worker heartbeat, from whichever process runs the jobs."""
    return await s.scalar(text("SELECT max(last_heartbeat) FROM jobs.procrastinate_workers"))  # type: ignore[no-any-return]


def register_health(app: FastAPI, service: jobs.JobService) -> None:
    """/healthz and /readyz: the admin API's, and the worker's own when the admin runs apart (main.py)."""

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Liveness: the process answers. No dependency checks."""
        return {"status": "ok", "runningJobs": service.running}

    @app.get("/readyz", response_model=None)
    async def readyz() -> dict[str, Any] | JSONResponse:
        """Readiness: the database answers too, within READY_TIMEOUT_S. 503 with the error's type when it
        doesn't."""
        try:
            async with asyncio.timeout(READY_TIMEOUT_S), get_sessionmaker()() as s:
                await s.execute(text("select 1"))
        except Exception as exc:
            log.warning("not ready: database %s", type(exc).__name__)
            body = {"status": "unavailable", "error": f"database: {type(exc).__name__}", "runningJobs": service.running}
            return JSONResponse(body, status_code=503)
        return {"status": "ok", "runningJobs": service.running}


def register(app: FastAPI, ctx: AdminContext) -> None:
    deps = ctx.deps
    settings = ctx.settings
    service = ctx.service
    auth = ctx.auth
    started_at = ctx.started_at
    viewer_check = ctx.viewer_check

    # ── Health / jobs ─────────────────────────────────────────────────────

    register_health(app, service)

    async def api_ok() -> bool:
        """Whether the api can serve: its /readyz, which checks its database too."""
        url = (settings.api_internal_url or f"http://127.0.0.1:{settings.api_port}").rstrip("/")
        try:
            resp = await http.get_client().get(f"{url}/readyz", timeout=3)
            return resp.status_code == 200
        except Exception:  # connection refused, timeout, ...
            return False

    @app.get("/admin/health", dependencies=auth)
    async def health() -> dict[str, Any]:
        """One call for the dashboard's status bar. The YouTube token is refreshed at
        most every 10 minutes here; /admin/youtube/status always refreshes it."""

        async def db_state() -> tuple[dict[str, int], list[Any], Stream | None, dt.datetime | None] | None:
            try:
                async with get_sessionmaker()() as s:
                    counts = await _job_counts(s)
                    failures = list(
                        (
                            await s.execute(
                                select(ALL_JOBS)
                                .where(ALL_JOBS.c.state == "failed")
                                .order_by(ALL_JOBS.c.updated_at.desc(), ALL_JOBS.c.id.desc())
                                .limit(5)
                            )
                        ).all()
                    )
                    live = (
                        await s.execute(
                            select(Stream).where(Stream.is_live.is_(True)).order_by(Stream.started_at.desc()).limit(1)
                        )
                    ).scalar_one_or_none()
                    beat = await _worker_beat(s) if settings.admin_apart else None
                    return counts, failures, live, beat
            except Exception:
                log.exception("health: database query failed")
                return None

        async def quota() -> dict[str, Any] | None:
            try:
                return await youtube_quota.today(settings.youtube_daily_quota)
            except Exception:
                log.exception("health: YouTube quota lookup failed")
                return None

        db, api, yt, viewer, used, bot = await asyncio.gather(
            db_state(),
            api_ok(),
            deps.youtube.cached_check(YOUTUBE_CHECK_MAX_AGE),
            viewer_check(),
            quota(),
            deps.doomtp.status(),
        )
        db_ok = db is not None
        counts, failures, live, beat = db or ({st: 0 for st in jobs.STATES}, [], None, None)
        if settings.admin_apart:
            # The jobs run in another process: it is up while its heartbeat is fresh, and the running
            # count is the database's. Its start time isn't recorded anywhere this process can see.
            fresh = beat is not None and (dt.datetime.now(dt.UTC) - beat).total_seconds() < HEARTBEAT_MAX_AGE_S
            worker = {"ok": db_ok and fresh, "runningJobs": counts.get("running", 0), "startedAt": None}
        else:
            worker = {"ok": db_ok, "runningJobs": service.running, "startedAt": iso_utc(started_at)}
        return {
            "worker": worker,
            "api": {"ok": api},
            "youtube": {
                "authorized": yt["authorized"],
                "valid": yt["valid"],
                "error": yt.get("error"),
                "channel": yt.get("channel"),
                "connectedAt": yt.get("connectedAt"),
                "refreshTokenExpiresAt": yt.get("refreshTokenExpiresAt"),
                "checkedAt": iso_utc(yt["checkedAt"]),
                "quota": used,
            },
            "twitchViewer": viewer,
            "doomtp": bot,
            "live": {
                "live": live is not None,
                "streamId": str(live.id) if live else None,
                "startedAt": iso_utc(live.started_at) if live else None,
            },
            "jobs": {
                "counts": counts,
                "recentFailures": [_job_json(j) for j in failures],
            },
        }

    @app.get("/admin/kinds", dependencies=auth)
    async def list_kinds() -> dict[str, Any]:
        """Job kinds, their steps, and the steps each pauses before by default."""
        return {
            kind: {"steps": steps, "manualSteps": settings.manual_steps.get(kind, [])}
            for kind, steps in jobs.KINDS.items()
        }

    # ── Audit log ─────────────────────────────────────────────────────────

    @app.get("/admin/audit", dependencies=auth)
    async def list_audit(before: int | None = None, limit: int = 50) -> dict[str, Any]:
        """Newest first; ``before`` (an entry id) pages back. What admins did: the worker's own actions
        (the monitor queueing a job) are left out."""
        c = AUDIT_LOG.c
        stmt = (
            select(AUDIT_LOG)
            .where(c.actor_kind.in_(("user", "api_key")))
            .order_by(c.id.desc())
            .limit(min(max(limit, 1), 500))
        )
        if before is not None:
            stmt = stmt.where(c.id < before)
        async with get_sessionmaker()() as s:
            rows = (await s.execute(stmt)).all()
        return {"data": [_audit_json(r) for r in rows]}
