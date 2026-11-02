"""``/vods?$cut=false`` leaves out the VODs cut in full, in the query, so totals and paging hold."""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from archive_api.main import create_app
from archive_common.config import get_settings
from archive_common.db import get_engine, get_sessionmaker
from archive_common.models import Vod
from sqlalchemy import delete, text

START = dt.datetime(2001, 5, 6, 20, 0, tzinfo=dt.UTC)  # before any real VOD


def _ch(restricted: bool | None) -> dict[str, Any]:
    ch: dict[str, Any] = {"gameId": "1", "name": "G", "image": None, "duration": "00:00:00", "start": 0, "end": 60}
    if restricted is not None:
        ch["restricted"] = restricted
    return ch


# id → chapters; "cut" is the only one with chapters that are all restricted
VODS = {
    "test-cut-cut": [_ch(True), _ch(True)],
    "test-cut-part": [_ch(True), _ch(False)],
    "test-cut-open": [_ch(False)],
    "test-cut-unset": [_ch(None)],  # older rows have no `restricted`
    "test-cut-none": [],
}
ALL = "&".join(f"id[$in][]={quote(i)}" for i in VODS)


@pytest.fixture
async def vods():
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("select 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")

    async def clean() -> None:
        async with get_sessionmaker()() as s:
            await s.execute(delete(Vod).where(Vod.id.like("test-cut-%")))
            await s.commit()

    await clean()
    async with get_sessionmaker()() as s:
        s.add_all(
            Vod(
                id=i,
                title="t",
                created_at=START + dt.timedelta(hours=n),
                duration="00:01:00",
                chapters=ch,
                tags=["vods"],
            )
            for n, (i, ch) in enumerate(VODS.items())
        )
        await s.commit()
    yield
    await clean()


async def _get(q: str) -> dict[str, Any]:
    settings = get_settings()
    settings.rate_limit_points = 1_000_000
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://t") as c:
        r = await c.get(f"/vods?{ALL}&{q}")
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_cut_false_leaves_out_vods_cut_in_full(vods):
    assert {v["id"] for v in (await _get("$limit=50"))["data"]} == set(VODS)
    assert {v["id"] for v in (await _get("$cut=true&$limit=50"))["data"]} == set(VODS)
    page = await _get("$cut=false&$limit=50")
    assert {v["id"] for v in page["data"]} == set(VODS) - {"test-cut-cut"}
    assert page["total"] == 4


async def test_cut_false_pages_and_counts(vods):
    page = await _get("$cut=false&$limit=2&$skip=2&$sort[createdAt]=1")
    assert page["total"] == 4
    assert [v["id"] for v in page["data"]] == ["test-cut-unset", "test-cut-none"]
