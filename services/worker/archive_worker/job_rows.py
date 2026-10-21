"""Jobs as the admin API and the other readers see them: vex-platform's ``jobs.job_runs`` and the
rows left in the legacy ``jobs`` table (legacy_jobs.py), read as one table.

A run's ``subject`` is ``vod:<id>``, read as ``vod_id``, and its ``succeeded`` state as ``done``,
so a row looks the same whichever table it came from. ``legacy`` says which one that was. Ids do
not collide: Alembic 0013 started ``job_runs`` above the legacy ids.
"""

from __future__ import annotations

from archive_common.db import get_sessionmaker
from archive_common.models import Job
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Row,
    Table,
    Text,
    case,
    func,
    literal,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

ACTIVE = ("queued", "running", "paused")
# The v1 admin API's names; job_runs says "succeeded" for "done".
STATES = ("queued", "running", "paused", "done", "failed", "cancelled")

RUNS = Table(
    "job_runs",
    MetaData(schema="jobs"),
    Column("id", BigInteger, primary_key=True),
    Column("kind", Text),
    Column("subject", Text),
    Column("state", Text),
    Column("step", Text),
    Column("payload", JSONB),
    Column("attempts", Integer),
    Column("last_error", Text),
    Column("not_before", DateTime(timezone=True)),
    Column("pause_before", ARRAY(Text)),
    Column("pause_next", Boolean),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("parent_id", BigInteger),
)


def subject_of(vod_id: str | None) -> str | None:
    return f"vod:{vod_id}" if vod_id else None


def vod_of(subject: str | None) -> str | None:
    return subject[4:] if subject and subject.startswith("vod:") else None


def _runs():  # type: ignore[no-untyped-def]
    r = RUNS.c
    return select(
        r.id,
        r.kind,
        case((r.subject.startswith("vod:"), func.substr(r.subject, 5))).label("vod_id"),
        case((r.state == "succeeded", "done"), else_=r.state).label("state"),
        r.step,
        r.attempts,
        r.last_error,
        r.payload,
        r.not_before,
        r.pause_before,
        r.pause_next,
        r.created_at,
        r.updated_at,
        literal(False).label("legacy"),
    )


def _legacy():  # type: ignore[no-untyped-def]
    j = Job.__table__.c
    return select(
        j.id,
        j.kind,
        j.vod_id,
        j.state,
        j.step,
        j.attempts,
        j.last_error,
        j.payload,
        j.not_before,
        j.pause_before,
        j.pause_next,
        j.created_at,
        j.updated_at,
        literal(True).label("legacy"),
    )


ALL_JOBS = union_all(_runs(), _legacy()).subquery("all_jobs")  # type: ignore[no-untyped-call]


# ── Reading jobs of both tables ────────────────────────────────────────────


def _matching(stmt, kind: str, vod_id: str | None, stream_id: str | None):  # type: ignore[no-untyped-def]
    stmt = stmt.where(ALL_JOBS.c.kind == kind)
    if vod_id is not None:
        stmt = stmt.where(ALL_JOBS.c.vod_id == vod_id)
    if stream_id is not None:
        stmt = stmt.where(ALL_JOBS.c.payload["stream_id"].astext == str(stream_id))
    return stmt.limit(1)


async def find_active(kind: str, *, vod_id: str | None = None, stream_id: str | None = None) -> Row | None:  # type: ignore[type-arg]
    stmt = _matching(select(ALL_JOBS).where(ALL_JOBS.c.state.in_(ACTIVE)), kind, vod_id, stream_id)
    async with get_sessionmaker()() as s:
        return (await s.execute(stmt)).first()  # type: ignore[no-any-return]


async def active_by_vod(vod_ids: list[str]) -> dict[str, list[Row]]:  # type: ignore[type-arg]
    """The queued, running and paused jobs of each of ``vod_ids`` that has any, oldest first."""
    if not vod_ids:
        return {}
    stmt = select(ALL_JOBS).where(ALL_JOBS.c.state.in_(ACTIVE), ALL_JOBS.c.vod_id.in_(vod_ids)).order_by(ALL_JOBS.c.id)
    out: dict[str, list[Row]] = {}  # type: ignore[type-arg]
    async with get_sessionmaker()() as s:
        for row in (await s.execute(stmt)).all():
            out.setdefault(row.vod_id, []).append(row)
    return out


async def exists_any(kind: str, *, vod_id: str | None = None, stream_id: str | None = None) -> bool:
    """Any job (including finished/failed) — used so the monitor enqueues once per stream."""
    async with get_sessionmaker()() as s:
        return (await s.execute(_matching(select(ALL_JOBS.c.id), kind, vod_id, stream_id))).first() is not None
