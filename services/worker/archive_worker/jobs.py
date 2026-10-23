"""The worker's jobs, run by vex-platform's ``JobRuntime`` (rows in ``jobs.job_runs``).

A job is a list of named steps (see ``KINDS``). ``step`` is the first step that has not finished:
the runtime advances it (together with ``payload``, which carries state between steps) only after a
step returns, so a restarted worker never repeats a finished step. A step interrupted part-way is
re-run, so steps must tolerate that (most checkpoint their own progress in the payload).

Manual gates: a job pauses (state ``paused``) when it is about to start a gated step, and waits
there until resumed. Gates come from the job's own ``pause_before`` or, when that is NULL,
``Settings.manual_steps[kind]`` (``apply_settings`` hands those to the registry). ``pause_next``
pauses at the next step boundary once. Gates are checked when a job moves on to a step, so
resuming, retrying or recovering a job runs its current step.

A job's subject is ``vod:<id>``; one job per (VOD, video type) runs at a time (``_lock``). Jobs
queued before the runtime existed (the old ``jobs`` table) are still listed and their logs read, but
nothing runs them or acts on them any more (ADR 0003, step 1).

Every action on a job is audited (``audit_log``) by the runtime, in the transaction that makes it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from archive_common import audit
from archive_common.config import Settings
from archive_common.db import get_sessionmaker
from sqlalchemy import Row, select
from sqlalchemy.engine import make_url
from vex_platform.actor import SYSTEM, Actor
from vex_platform.jobs import InvalidJob, JobConflict, JobNotFound, JobRun, JobRuntime, Registry

from .context import Deps, JobContext
from .events import event_json, iso_utc
from .job_rows import ACTIVE, ALL_JOBS, STATES, active_by_vod, exists_any, find_active, subject_of, vod_of
from .kinds import KINDS
from .steps import STEPS

__all__ = [
    "ACTIVE",
    "KINDS",
    "STATES",
    "InvalidJob",
    "JobConflict",
    "JobNotFound",
    "JobService",
    "active_by_vod",
    "apply_settings",
    "build_registry",
    "check_steps",
    "create_runtime",
    "exists_any",
    "find_active",
    "get",
]

# The old runner waited 60·2^n seconds after the n-th failure; the runtime waits base·2^(n-1).
RETRY_BASE_SECONDS = 120


def check_steps(kind: str, steps: list[str]) -> None:
    """InvalidJob unless ``kind`` exists and every name is one of its steps."""
    if kind not in KINDS:
        raise InvalidJob(f"unknown job kind {kind!r}; kinds: {', '.join(KINDS)}")
    unknown = [s for s in steps if s not in KINDS[kind]]
    if unknown:
        raise InvalidJob(f"{kind!r} has no step(s) {', '.join(unknown)}; steps: {', '.join(KINDS[kind])}")


def _lock(run: JobRun) -> str | None:
    # One job per (vod, video type) at a time; the live recording and the VOD
    # capture of the same stream run side by side, and bot chat beside the archive steps. A backfill's
    # bot_chat runs share one lock, so they go one at a time and live jobs never wait behind many.
    if run.kind == "bot_chat" and run.payload.get("backfill"):
        return "bot_chat_backfill"
    if run.kind == "previews_fetch":
        return "previews_fetch"  # one YouTube download at a time, whatever the VOD
    vod_id = vod_of(run.subject)
    if vod_id is None:
        return None
    typ = "bot_chat" if run.kind == "bot_chat" else run.payload.get("type", "vod")
    return f"vod:{vod_id}:{typ}"


def build_registry(manual_steps: dict[str, list[str]] | None = None) -> Registry:
    registry = Registry()
    for name, fn in STEPS.items():
        registry.add_step(name, fn)
    for kind, steps in KINDS.items():
        registry.kind(
            kind,
            steps,
            lock=_lock,
            retry_base_seconds=RETRY_BASE_SECONDS,
            pause_before=tuple((manual_steps or {}).get(kind, ())),
        )
    return registry


def apply_settings(runtime: JobRuntime, settings: Settings) -> None:
    """The runtime settings the runtime reads (runtime_settings.py): concurrency, attempts, gates."""
    runtime.set_concurrency(settings.runner_concurrency)
    runtime.max_attempts = settings.max_attempts
    for kind in KINDS:
        runtime.registry.set_pause_before(kind, settings.manual_steps.get(kind, []))


def conninfo(database_url: str) -> str:
    """The SQLAlchemy URL (postgresql+asyncpg://...) as a libpq one for the runtime's psycopg pool."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def create_runtime(deps: Deps, **options: Any) -> JobRuntime:
    """The runtime of every kind in ``KINDS``; ``options`` go to ``JobRuntime``."""
    settings = deps.settings
    for kind, steps in settings.manual_steps.items():
        check_steps(kind, steps)  # fail at startup on a typo in ARCHIVE_MANUAL_STEPS
    return JobRuntime(
        build_registry(settings.manual_steps),
        conninfo(settings.database_url),
        concurrency=settings.runner_concurrency,
        max_attempts=settings.max_attempts,
        context_factory=lambda ctx: JobContext.of_run(ctx, deps),
        **{"audit_table": audit.TABLE, **options},
    )


async def get(job_id: int) -> Row:  # type: ignore[type-arg]
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(ALL_JOBS).where(ALL_JOBS.c.id == job_id).order_by(ALL_JOBS.c.legacy).limit(1))
        ).first()
    if row is None:
        raise JobNotFound(job_id)
    return row  # type: ignore[no-any-return]


# ── Acting on jobs ─────────────────────────────────────────────────────────

_RUN_CONFLICT = re.compile(r"run \d+ is (\w+); only (.+) runs can be (\w+)")


@contextmanager
def _v1_conflicts() -> Iterator[None]:
    """The runtime's conflicts ("run 7 is succeeded; only paused runs can be resumed") worded as the
    admin API always has ("Job is done; only paused jobs can be resumed")."""
    try:
        yield
    except JobConflict as exc:
        m = _RUN_CONFLICT.fullmatch(str(exc))
        if m is None:
            raise
        state = "done" if m[1] == "succeeded" else m[1]
        raise JobConflict(f"Job is {state}; only {m[2]} jobs can be {m[3]}") from exc


async def _not_legacy(job_id: int) -> None:
    """Nothing runs the old table's jobs any more: they can be read, not acted on."""
    if (await get(job_id)).legacy:
        raise JobConflict(f"Job {job_id} is from the old jobs table, which no longer runs; queue a new job instead")


@dataclass
class JobService:
    """What the admin API, the monitor and the CLI do with jobs: new ones go to the runtime, and so do
    the actions on them. Each returns the job as ``get`` reads it."""

    deps: Deps
    runtime: JobRuntime

    @classmethod
    def create(cls, deps: Deps) -> JobService:
        return cls(deps, create_runtime(deps))

    @property
    def running(self) -> int:
        """Jobs running in this worker now."""
        return self.runtime.limiter.active  # type: ignore[no-any-return]

    def apply_settings(self) -> None:
        apply_settings(self.runtime, self.deps.settings)

    async def enqueue(
        self,
        kind: str,
        vod_id: str | None,
        payload: dict[str, Any] | None = None,
        *,
        actor: Actor = SYSTEM,
        step: str | None = None,
        pause_before: list[str] | None = None,
        paused: bool = False,
    ) -> Row:  # type: ignore[type-arg]
        """Queue a job at ``step`` (default: its first). ``paused`` holds it until resumed."""
        enqueued = await self.runtime.enqueue(
            kind, subject_of(vod_id), payload, actor=actor, step=step, pause_before=pause_before, paused=paused
        )
        return await get(enqueued.run.id)

    async def resume(self, job_id: int, *, once: bool = False, actor: Actor = SYSTEM) -> Row:  # type: ignore[type-arg]
        await _not_legacy(job_id)
        with _v1_conflicts():
            await self.runtime.resume(job_id, once=once, actor=actor)
        return await get(job_id)

    async def pause(self, job_id: int, *, actor: Actor = SYSTEM) -> Row:  # type: ignore[type-arg]
        await _not_legacy(job_id)
        with _v1_conflicts():
            await self.runtime.pause(job_id, actor=actor)
        return await get(job_id)

    async def retry(self, job_id: int, *, actor: Actor = SYSTEM) -> Row:  # type: ignore[type-arg]
        await _not_legacy(job_id)
        with _v1_conflicts():
            await self.runtime.retry(job_id, actor=actor)
        return await get(job_id)

    async def cancel(self, job_id: int, *, actor: Actor = SYSTEM) -> Row:  # type: ignore[type-arg]
        await _not_legacy(job_id)
        with _v1_conflicts():
            await self.runtime.cancel(job_id, actor=actor)
        return await get(job_id)

    async def update(self, job_id: int, *, actor: Actor = SYSTEM, **values: Any) -> Row:  # type: ignore[type-arg]
        """``pause_before`` (None: back to the kind's default) and/or ``pause_next``."""
        await _not_legacy(job_id)
        with _v1_conflicts():
            await self.runtime.update(job_id, actor=actor, **values)
        return await get(job_id)

    def note(self, job: Row, message: str) -> None:  # type: ignore[type-arg]
        """A line in the job's event log (an admin action on it)."""
        self.runtime.events.add(job.id, "info", job.step, message)

    async def events(self, job_id: int, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """The job's log lines, step changes and progress, oldest first, as ``event_json`` shapes them."""
        if (await get(job_id)).legacy:
            return [event_json(e) for e in await self.deps.events.list(job_id, after=after, limit=limit)]
        return [
            {
                "seq": e["id"],
                "at": iso_utc(e["at"]),
                "level": e["level"],
                "step": e["step"],
                "message": e["message"],
                "progress": e["progress"],
            }
            for e in await self.runtime.events.list(job_id, after=after, limit=limit)
        ]
