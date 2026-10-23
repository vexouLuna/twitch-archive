"""/admin's merge candidates, and the row-moving merge and split answering 410 (ADR 0004; needs the dev DB)."""

import datetime as dt

import httpx
import pytest
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_worker.admin import create_admin_app
from pydantic import SecretStr
from sqlalchemy import delete

A, B = "test-merge-a", "test-merge-b"
KEY = {"Authorization": "Bearer k"}
START = dt.datetime(2001, 2, 3, 20, 0, tzinfo=dt.UTC)  # before any real VOD


@pytest.fixture
async def admin(db, deps, make_service):
    async def clean() -> None:
        async with get_sessionmaker()() as s:
            await s.execute(delete(Vod).where(Vod.id.in_([A, B])))
            await s.commit()

    await clean()
    async with get_sessionmaker()() as s:
        s.add(Vod(id=A, title="big stream", created_at=START, duration="02:00:00", stream_id="1001"))
        s.add(
            Vod(
                id=B,
                title="big stream ",
                created_at=START + dt.timedelta(seconds=7500),
                duration="01:00:00",
                stream_id="1002",
            )
        )
        await s.commit()
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        yield c
    await clean()


async def test_merge_candidates(admin):
    got = (await admin.get(f"/admin/vods/{A}/merge-candidates", headers=KEY)).json()
    assert got["withinMinutes"] == 30 and got["vod"]["endsAt"] == "2001-02-03T22:00:00+00:00"
    assert got["candidates"] == [
        {
            "id": B,
            "streamId": "1002",
            "title": "big stream ",
            "duration": "01:00:00",
            "createdAt": "2001-02-03T22:05:00+00:00",
            "gap": 300,
            "overlaps": False,
            "titlesMatch": True,
        }
    ]
    assert (await admin.get(f"/admin/vods/{B}/merge-candidates", headers=KEY)).json()["candidates"] == []
    assert (await admin.get("/admin/vods/nope/merge-candidates", headers=KEY)).status_code == 404


@pytest.mark.parametrize("route", ["merge", "unmerge", "split", "unsplit"])
async def test_row_moving_merge_and_split_are_gone(admin, route):
    r = await admin.post(f"/admin/vods/{A}/{route}", headers=KEY, json={"source": B, "at": 60})
    assert r.status_code == 410 and "/api/v2/" in r.json()["msg"]
    assert (await admin.post(f"/admin/vods/{A}/{route}", json={})).status_code == 403  # still behind auth (v1: 403)
