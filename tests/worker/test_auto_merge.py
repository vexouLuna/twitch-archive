"""``synthetic.auto_merge``: a new VOD merged at ingest with the one before it (needs the dev DB).

A (2 h), then B 10 min after A ended, then C 5 min after B ended, then D an hour after C ended.
"""

import datetime as dt

import pytest
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import Vod, VodSegment
from archive_common.segments import Segment
from archive_worker import synthetic
from sqlalchemy import delete, select

A, B, C, D = (f"test-am-{x}" for x in "abcd")
M = f"{A}-m"
START = dt.datetime(2000, 1, 2, 20, 0, tzinfo=dt.UTC)  # before any real VOD


async def _clean():
    async with get_sessionmaker()() as s:
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.like("vod:test-am-%")))
        await s.execute(delete(VodSegment).where(VodSegment.vod_id.like("test-am-%")))
        await s.execute(delete(Vod).where(Vod.id.like("test-am-%"), Vod.synthetic.is_not(None)))
        await s.execute(delete(Vod).where(Vod.id.like("test-am-%")))
        await s.commit()


@pytest.fixture
async def vods(db):
    await _clean()
    at = {A: 0, B: 2 * 3600 + 600, C: 3 * 3600 + 900, D: 5 * 3600 + 900}
    async with get_sessionmaker()() as s:
        for vod_id, offset in at.items():
            length = "02:00:00" if vod_id == A else "01:00:00"
            s.add(Vod(id=vod_id, title="stream", created_at=START + dt.timedelta(seconds=offset), duration=length))
        await s.commit()
    yield
    await _clean()


async def _plays(vod_id):
    async with get_sessionmaker()() as s:
        rows = (
            await s.execute(select(VodSegment).where(VodSegment.vod_id == vod_id).order_by(VodSegment.pos))
        ).scalars()
        return [r.source_id for r in rows]


async def test_auto_merge(vods):
    assert await synthetic.auto_merge(A, 15) is None  # nothing before it
    assert await synthetic.auto_merge(B, 5) is None  # a 10 min break
    assert await synthetic.auto_merge(B, 15) == M
    assert await _plays(M) == [A, B]
    assert await synthetic.auto_merge(B, 15) is None  # merged already
    assert await synthetic.auto_merge(C, 15) == M  # the merge grows
    assert await _plays(M) == [A, B, C]
    assert await synthetic.auto_merge(D, 15) is None  # an hour's break
    async with get_sessionmaker()() as s:
        hidden = {i: h for i, h in await s.execute(select(Vod.id, Vod.hidden).where(Vod.id.in_([A, B, C, D])))}
        audited = (await s.execute(select(AUDIT_LOG.c.action).where(AUDIT_LOG.c.target == f"vod:{M}"))).scalars().all()
    assert hidden == {A: True, B: True, C: True, D: False}
    assert sorted(audited) == ["synthetic.create", "synthetic.update"]


async def test_auto_merge_leaves_an_edited_merge(vods):
    assert await synthetic.auto_merge(B, 15) == M
    async with get_sessionmaker()() as s:
        segments = await synthetic._segments(s, M)
    await synthetic.change(M, segments=[segments[0], Segment(B, 0.0, None, 7300.0)])
    assert await synthetic.auto_merge(C, 15) is None
    assert await _plays(M) == [A, B]
