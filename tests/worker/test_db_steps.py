"""Steps against the local Postgres (skipped without it; see docs/development.md)."""

import asyncio
import datetime as dt
import json

import httpx
import respx
from archive_common.db import get_sessionmaker
from archive_common.models import Job, Log, Vod
from archive_common.twitch.gql import GQL_URL
from archive_worker.steps import metadata
from sqlalchemy import delete, select

VOD = "test-worker-vod"


async def _reset():
    async with get_sessionmaker()() as s:
        await s.execute(delete(Log).where(Log.vod_id == VOD))
        await s.execute(delete(Job).where(Job.vod_id == VOD))
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


async def _vod():
    await _reset()
    async with get_sessionmaker()() as s:
        s.add(Vod(id=VOD, title="t", created_at=dt.datetime.now(dt.UTC), duration="00:10:00"))
        await s.commit()


def _node(cid: str, offset: int, color: str | None = "#FF0000"):
    return {
        "id": cid,
        "contentOffsetSeconds": offset,
        "createdAt": "2026-02-21T03:00:00.123Z",
        "commenter": {"displayName": "someone"},
        "message": {"fragments": [{"text": "hi", "emote": None}], "userBadges": [], "userColor": color},
    }


PAGES = {
    None: {
        "edges": [
            {"cursor": "c1", "node": _node("00000000-0000-0000-0000-000000000001", 1)},
            {"cursor": "c2", "node": _node("00000000-0000-0000-0000-000000000002", 5, None)},
        ],
        "pageInfo": {"hasNextPage": True},
    },
    "c2": {
        "edges": [{"cursor": "c3", "node": _node("00000000-0000-0000-0000-000000000003", 9)}],
        "pageInfo": {"hasNextPage": False},
    },
}


def _gql(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    page = PAGES[body["variables"].get("cursor")]
    return httpx.Response(200, json={"data": {"video": {"comments": page}}})


async def _nosleep(*_a, **_k):
    return None


@respx.mock
async def test_chat_crawl_is_idempotent(db, make_ctx, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    await _vod()
    route = respx.post(GQL_URL).mock(side_effect=_gql)
    ctx = make_ctx("chat", VOD)
    await metadata.chat(ctx)
    await metadata.chat(ctx)  # resumes from the max stored offset; duplicates are ignored
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(Log).where(Log.vod_id == VOD).order_by(Log.seq))).scalars().all()
    assert [r.content_offset_seconds for r in rows] == [1, 5, 9]
    assert rows[1].user_color == "#999999"
    assert rows[0].message == [{"text": "hi", "emote": None}]
    # a run starts by offset (main client) and continues by cursor (backup client)
    first, second = route.calls[0].request, route.calls[1].request
    assert json.loads(first.content)["variables"] == {"videoID": VOD, "contentOffsetSeconds": 0}
    assert json.loads(second.content)["variables"] == {"videoID": VOD, "cursor": "c2"}
    assert first.headers["Client-Id"] != second.headers["Client-Id"]
    assert json.loads(route.calls[2].request.content)["variables"]["contentOffsetSeconds"] == 9
    await _reset()


async def test_upsert_vod_sets_helix_fields_only_on_insert(db):
    from archive_worker.vods import upsert_vod

    await _reset()
    video = {
        "id": VOD,
        "title": "first",
        "created_at": "2026-02-21T03:00:00Z",
        "duration": "1h2m3s",
        "thumbnail_url": "https://thumb/1",
    }
    try:
        await upsert_vod(video)
        await upsert_vod({**video, "title": "renamed", "duration": "9h", "thumbnail_url": ""})
        async with get_sessionmaker()() as s:
            vod = await s.get(Vod, VOD)
        assert (vod.title, vod.duration, vod.thumbnail_url) == ("renamed", "01:02:03", "https://thumb/1")
    finally:
        await _reset()
