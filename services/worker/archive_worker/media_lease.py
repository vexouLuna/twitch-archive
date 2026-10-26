"""One dashboard tab at a time streams a VOD's server file (``media_leases``, Alembic 0023).

A tab claims the lease with an id it made up (each tab its own), renews it every ``HEARTBEAT`` seconds
and releases it when it closes. A lease not renewed for ``TTL`` seconds is free. A claim succeeds when
the lease is free, already this tab's, or ``take_over`` is set (the other tab then loses it: its next
heartbeat or chunk is refused). The claim is one upsert, so two tabs can't both win.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import MediaLease
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from vex_platform.actor import Actor

SCOPE = "server"  # one viewer for the whole server: the disk and the uplink are shared
TTL = 30.0
HEARTBEAT = 10.0
LEASE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def owner_of(actor: Actor) -> str:
    return f"{actor.kind}:{actor.id}" if actor.id else actor.kind


_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
_SYSTEMS = (("iPhone", "iOS"), ("iPad", "iOS"), ("Android", "Android"), ("Windows", "Windows"), ("Mac OS X", "macOS"))


def browser_of(user_agent: str | None) -> str:
    """``Firefox on Windows`` from a User-Agent, for the in-use warning (only the family and the system)."""
    ua = user_agent or ""
    name = next((n for key, n in _BROWSERS if key in ua), "A browser")
    system = next((s for key, s in _SYSTEMS if key in ua), "Linux" if "Linux" in ua else None)
    return f"{name} on {system}" if system else name


@dataclass(frozen=True)
class Lease:
    lease_id: str
    owner: str
    holder: dict[str, Any]
    acquired_at: dt.datetime
    heartbeat_at: dt.datetime

    def fresh(self, now: dt.datetime) -> bool:
        return (now - self.heartbeat_at).total_seconds() < TTL


def _lease(row: MediaLease) -> Lease:
    return Lease(row.lease_id, row.owner, dict(row.holder), row.acquired_at, row.heartbeat_at)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def current() -> Lease | None:
    """The lease if someone holds it (a stale one is nobody's)."""
    async with get_sessionmaker()() as s:
        row = await s.get(MediaLease, SCOPE)
    lease = _lease(row) if row else None
    return lease if lease and lease.fresh(_now()) else None


async def claim(
    lease_id: str, owner: str, holder: dict[str, Any], *, take_over: bool = False
) -> tuple[Lease | None, Lease | None]:
    """``(mine, other)``: the lease now held by ``lease_id``, or None and the fresh lease of whoever has it.
    With ``take_over`` ``other`` is who lost it (None when nobody held it)."""
    now = _now()
    stale = now - dt.timedelta(seconds=TTL)
    async with get_sessionmaker()() as s, s.begin():
        before_row = await s.get(MediaLease, SCOPE, with_for_update=True)
        before = _lease(before_row) if before_row else None
        new = insert(MediaLease).values(
            scope=SCOPE, lease_id=lease_id, owner=owner, holder=holder, acquired_at=now, heartbeat_at=now
        )
        same = MediaLease.lease_id == new.excluded.lease_id
        stmt = new.on_conflict_do_update(
            index_elements=[MediaLease.scope],
            set_={
                "lease_id": new.excluded.lease_id,
                "owner": new.excluded.owner,
                "holder": new.excluded.holder,
                "acquired_at": case((same, MediaLease.acquired_at), else_=new.excluded.acquired_at),
                "heartbeat_at": new.excluded.heartbeat_at,
            },
            where=None if take_over else or_(MediaLease.heartbeat_at < stale, same),
        ).returning(MediaLease)
        # populate_existing: the row is already in the session (the get above); RETURNING must overwrite it.
        row = (await s.execute(stmt, execution_options={"populate_existing": True})).scalar_one_or_none()
    other = before if before and before.lease_id != lease_id and before.fresh(now) else None
    return (_lease(row) if row else None), other


async def heartbeat(lease_id: str) -> bool:
    """Renew the lease; False when this tab no longer holds it (taken over, or it went stale and was taken)."""
    async with get_sessionmaker()() as s, s.begin():
        stmt = (
            update(MediaLease)
            .where(MediaLease.scope == SCOPE, MediaLease.lease_id == lease_id)
            .values(heartbeat_at=_now())
            .returning(MediaLease.scope)
        )
        return (await s.execute(stmt)).first() is not None


async def release(lease_id: str) -> None:
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(delete(MediaLease).where(MediaLease.scope == SCOPE, MediaLease.lease_id == lease_id))


async def holds(lease_id: str) -> bool:
    """Whether ``lease_id`` holds a fresh lease (the stream checks it as it goes)."""
    stale = _now() - dt.timedelta(seconds=TTL)
    async with get_sessionmaker()() as s:
        stmt = select(MediaLease.scope).where(
            MediaLease.scope == SCOPE, MediaLease.lease_id == lease_id, MediaLease.heartbeat_at >= stale
        )
        return (await s.execute(stmt)).first() is not None
