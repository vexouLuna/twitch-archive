"""What the admin API's modules share: its errors, answers and helpers, and ``AdminContext``.

admin.py builds the app and its auth; the routes live in admin_status, admin_site, admin_jobs,
admin_vods, admin_twitch, admin_pipelines and admin_youtube, each a ``register(app, ctx)``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from archive_common.audit import legacy_actor
from archive_common.models import Vod
from sqlalchemy import func, select
from vex_platform.actor import SYSTEM, Actor

from . import (
    jobs,
)
from .admin_auth import (
    Session,
)
from .context import Deps
from .events import iso_utc
from .job_rows import ALL_JOBS
from .runtime_settings import RuntimeSettings
from .storage import Storage

if TYPE_CHECKING:
    from archive_common.config import Settings
    from archive_common.twitch.helix import Helix

    from .context import Deps
    from .jobs import JobService

log = logging.getLogger("archive_worker.admin")  # one logger for the whole admin API, as before
READY_TIMEOUT_S = 5  # /readyz: a database slower than this counts as down

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SESSION_COOKIE_ARGS = {"path": "/", "secure": True, "httponly": True, "samesite": "strict"}
# The password login over plain HTTP (a LAN address with no TLS): a browser drops a Secure cookie
# set over HTTP, so there it goes without. The password only works from the local network anyway.
LAN_SESSION_COOKIE_ARGS = {**SESSION_COOKIE_ARGS, "secure": False}
STATE_COOKIE_ARGS = {"path": "/", "secure": True, "httponly": True, "samesite": "lax"}
SESSION_PATH = "/admin/session"  # the one route a site's page may call from its own origin (CORS)
CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE",
    "Access-Control-Allow-Headers": "Content-Type, X-CSRF-Token",
    "Access-Control-Max-Age": "600",
}
AUDITED_PREFIXES = ("/admin/", "/v2/")
JOB_ROUTES = "/admin/jobs"  # audited by the runtime (and JobService), not by the request
YOUTUBE_CHECK_MAX_AGE = 600  # /admin/health refreshes the YouTube token at most this often
RECENT_JOBS = 20  # jobs shown with a VOD
VOD_LIST_MAX = 200  # GET /admin/vods?limit=
# Steps that refetch from Twitch by VOD id; a job with any of them is refused on a merged/split VOD.
TWITCH_STEPS = {"capture", "fetch_vod", "finalize", "chapters", "chat", "emotes", "bot_chat"}


class AdminError(Exception):
    def __init__(
        self, status: int, msg: str, headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None
    ) -> None:
        self.status, self.msg, self.headers, self.extra = status, msg, headers, extra or {}


class Unauthenticated(AdminError):
    """No credentials, or a session that has ended: 403 in v1 as always, 401 in v2."""

    def __init__(self, msg: str) -> None:
        super().__init__(403, msg)


def _ok(msg: str, job: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": False, "msg": msg, **extra}
    if job is not None:
        out["jobId"] = job.id
    return out


def _step_names(value: Any, msg: str) -> list[str] | None:
    """``pauseBefore``: a list of step names, or None."""
    if value is not None and not (isinstance(value, list) and all(isinstance(s, str) for s in value)):
        raise AdminError(400, msg)
    return value


async def _job_counts(s) -> dict[str, int]:  # type: ignore[no-untyped-def]
    counts = dict((await s.execute(select(ALL_JOBS.c.state, func.count()).group_by(ALL_JOBS.c.state))).all())
    return {st: counts.get(st, 0) for st in jobs.STATES}


def _require(body: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if body.get(key) in (None, ""):
            raise AdminError(400, f"Missing parameter: {key}")


# Shorthands accepted by GET /admin/jobs?state=
STATE_GROUPS = {
    "waiting": ("queued",),  # will run on its own (possibly after a retry backoff)
    "stopped": ("paused", "failed", "cancelled"),  # needs someone to resume/retry
    "active": jobs.ACTIVE,
    "finished": ("done", "failed", "cancelled"),
}


def _states(value: str) -> list[str]:
    out: list[str] = []
    for name in (v.strip() for v in value.split(",") if v.strip()):
        group = STATE_GROUPS.get(name, (name,))
        for state in group:
            if state not in jobs.STATES:
                raise AdminError(
                    400, f"Unknown state {state!r}; states: {', '.join(jobs.STATES)}, groups: {', '.join(STATE_GROUPS)}"
                )
            out.append(state)
    return out


def login_of(session: Session) -> str | None:
    """The Twitch login behind a session, for the audit log (None for the password)."""
    return (session.user or {}).get("login") or None


def _audit_json(row: Any) -> dict[str, Any]:
    """An ``audit_log`` row as GET /admin/audit has always shown one (``admin_audit``'s shape)."""
    detail = row.detail
    if row.before is not None or row.after is not None:
        detail = {"before": row.before, "after": row.after}
    return {
        "id": row.id,
        "at": iso_utc(row.at),
        "actor": legacy_actor(row.actor_kind, row.actor_id),
        "actorLogin": row.actor_login,
        "action": row.action,
        "target": row.target,
        "detail": detail,
    }


# Who the request being handled is (set by ``verify``), for the jobs it queues or acts on.
_actor: ContextVar[Actor] = ContextVar("admin_actor", default=SYSTEM)


def _job_json(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "vodId": job.vod_id,
        "state": job.state,
        "step": job.step,
        "attempts": job.attempts,
        "lastError": job.last_error,
        "payload": job.payload,
        "notBefore": iso_utc(job.not_before),
        "pauseBefore": job.pause_before,
        "pauseNext": job.pause_next,
        "steps": jobs.KINDS.get(job.kind, []),
        "createdAt": iso_utc(job.created_at),
        "updatedAt": iso_utc(job.updated_at),
    }


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of an absolute URL, lowercased; empty for anything else."""
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


@dataclass(frozen=True)
class AdminContext:
    """What create_admin_app hands each route module: the worker's dependencies, ``auth`` (the
    dependency every /admin route takes) and the helpers several modules use."""

    deps: Deps
    settings: Settings
    service: JobService
    runtime: RuntimeSettings
    storage: Storage
    helix: Helix
    auth: list[Any]
    started_at: dt.datetime
    enqueue: Callable[..., Awaitable[Any]]
    job_action: Callable[[str, Any], dict[str, Any]]
    vod_exists: Callable[[str], Awaitable[Vod | None]]
    require_vod: Callable[[str], Awaitable[Vod]]
    refuse_spliced: Callable[[str, str], Awaitable[None]]
    require_helix: Callable[[], None]
    helix_video: Callable[[str], Awaitable[dict[str, Any]]]
    type_payload: Callable[[Vod, dict[str, Any]], dict[str, Any]]
    source_payload: Callable[[Vod, dict[str, Any]], dict[str, Any]]
    single_part: Callable[[dict[str, Any], dict[str, Any]], int]
    viewer_check: Callable[..., Awaitable[dict[str, Any] | None]]
