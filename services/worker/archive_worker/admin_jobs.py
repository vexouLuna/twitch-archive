"""Jobs: listing, queueing and acting on them (/admin/jobs)."""

from __future__ import annotations

from typing import Any

from archive_common.db import get_sessionmaker
from fastapi import Body, FastAPI
from sqlalchemy import select

from . import (
    jobs,
)
from .admin_shared import (
    AdminContext,
    AdminError,
    _actor,
    _job_counts,
    _job_json,
    _ok,
    _require,
    _states,
    _step_names,
)
from .job_rows import ALL_JOBS


def register(app: FastAPI, ctx: AdminContext) -> None:
    service = ctx.service
    auth = ctx.auth
    enqueue = ctx.enqueue
    job_action = ctx.job_action
    require_vod = ctx.require_vod

    @app.get("/admin/jobs", dependencies=auth)
    async def list_jobs(
        state: str | None = None,
        vodId: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        before: int | None = None,
    ) -> dict[str, Any]:
        """``state`` takes a comma-separated list of states and/or groups (see STATE_GROUPS).
        Newest first; ``before`` (a job id) pages back through older ones."""
        states = _states(state) if state else None
        c = ALL_JOBS.c
        async with get_sessionmaker()() as s:
            stmt = select(ALL_JOBS).order_by(c.id.desc(), c.legacy).limit(min(max(limit, 1), 500))
            if before is not None:
                stmt = stmt.where(c.id < before)
            if states:
                stmt = stmt.where(c.state.in_(states))
            if vodId:
                stmt = stmt.where(c.vod_id == vodId)
            if kind:
                stmt = stmt.where(c.kind == kind)
            rows = (await s.execute(stmt)).all()
            counts = await _job_counts(s)
        return {"counts": counts, "data": [_job_json(j) for j in rows]}

    @app.post("/admin/jobs", dependencies=auth)
    async def launch_job(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Start any job kind. Body: kind, vodId?, payload?, fromStep?, pauseBefore?, paused?"""
        _require(body, "kind")
        vod_id = str(body["vodId"]) if body.get("vodId") not in (None, "") else None
        if vod_id is not None:
            await require_vod(vod_id)
        pause_before = _step_names(body.get("pauseBefore"), "pauseBefore must be a list of step names")
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            raise AdminError(400, "payload must be an object")
        job = await enqueue(
            str(body["kind"]),
            vod_id,
            payload,
            step=body.get("fromStep") or None,
            pause_before=pause_before,
            paused=bool(body.get("paused")),
        )
        return _ok(f"Job {job.id} {job.kind} {job.state} at step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/resume", dependencies=auth)
    async def resume_job(job_id: int, body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """Run a paused job from its current step. ``{"once": true}`` pauses again after it."""
        job = await service.resume(job_id, once=bool((body or {}).get("once")), actor=_actor.get())
        return job_action(f"Job {job_id} resumed at step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/pause", dependencies=auth)
    async def pause_job(job_id: int) -> dict[str, Any]:
        job = await service.pause(job_id, actor=_actor.get())
        if job.state == "running":
            return job_action(f"Job {job_id} will pause when step {job.step} finishes", job)
        return job_action(f"Job {job_id} paused at step {job.step}", job)

    @app.get("/admin/jobs/{job_id}", dependencies=auth)
    async def get_job(job_id: int) -> dict[str, Any]:
        return _job_json(await jobs.get(job_id))

    @app.patch("/admin/jobs/{job_id}", dependencies=auth)
    async def patch_job(job_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """``pauseBefore`` (step names, or null for the ARCHIVE_MANUAL_STEPS default) and/or
        ``pauseNext``. Like any gate, they apply when the job next moves on to a step."""
        unknown = sorted(set(body) - {"pauseBefore", "pauseNext"})
        if unknown or not body:
            raise AdminError(
                400, "Body takes pauseBefore and/or pauseNext" + (f"; unknown: {', '.join(unknown)}" if unknown else "")
            )
        values: dict[str, Any] = {}
        if "pauseBefore" in body:
            values["pause_before"] = _step_names(
                body["pauseBefore"], "pauseBefore must be a list of step names or null"
            )
        if "pauseNext" in body:
            if not isinstance(body["pauseNext"], bool):
                raise AdminError(400, "pauseNext must be true or false")
            values["pause_next"] = body["pauseNext"]
        job = await service.update(job_id, actor=_actor.get(), **values)
        service.note(job, f"manual steps set: pauseBefore={job.pause_before}, pauseNext={job.pause_next}")
        return _job_json(job)

    @app.get("/admin/jobs/{job_id}/events", dependencies=auth)
    async def job_events(job_id: int, after: int = 0, limit: int = 200) -> dict[str, Any]:
        """The job's log lines, step changes and progress, oldest first. Poll with
        ``after=<next>`` from the previous answer to get only what is new."""
        rows = await service.events(job_id, after=after, limit=min(max(limit, 1), 1000))
        return {"data": rows, "next": rows[-1]["seq"] if rows else after}

    @app.post("/admin/jobs/{job_id}/retry", dependencies=auth)
    async def retry_job(job_id: int) -> dict[str, Any]:
        job = await service.retry(job_id, actor=_actor.get())
        return job_action(f"Job {job_id} re-queued from step {job.step}", job)

    @app.post("/admin/jobs/{job_id}/cancel", dependencies=auth)
    async def cancel_job(job_id: int) -> dict[str, Any]:
        job = await service.cancel(job_id, actor=_actor.get())
        return job_action(f"Job {job_id} cancelled", job)
