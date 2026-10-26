"""The VOD preview's server files: paths, ranges, the one-tab lease and its take-over (the routes need the dev DB)."""

import io
import os
import sys

import pytest
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import MediaLease
from archive_worker import api_v2_media, media_lease
from archive_worker.api_v2_media import MediaName, media_path
from sqlalchemy import delete, select
from vex_platform.api import ApiError

from .test_api_v2 import KEY, VOD, admin, vod  # noqa: F401 - fixtures

TAB_A, TAB_B = "tab-a-0123456789abcdef", "tab-b-0123456789abcdef"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"


def test_media_paths_stay_inside_the_vod_dir(tmp_path):
    vods = tmp_path / "vods"
    (vods / "123" / "censor").mkdir(parents=True)
    (vods / "123" / "123.mp4").write_bytes(b"x")
    (vods / "123" / "censor" / "123-censored.mp4").write_bytes(b"y")
    assert media_path(vods, "123", MediaName.source) == vods / "123" / "123.mp4"
    assert media_path(vods, "123", MediaName.censored) == vods / "123" / "censor" / "123-censored.mp4"
    assert media_path(vods, "123", MediaName.covered) is None  # not there
    assert media_path(vods, "..", MediaName.source) is None
    assert media_path(vods, "../123", MediaName.source) is None
    assert media_path(vods, "nope", MediaName.source) is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_media_paths_refuse_symlinks(tmp_path):
    vods = tmp_path / "vods"
    (vods / "1").mkdir(parents=True)
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"s")
    os.symlink(secret, vods / "1" / "1.mp4")
    assert media_path(vods, "1", MediaName.source) is None
    os.symlink(tmp_path, vods / "2")
    (tmp_path / "2.mp4").write_bytes(b"s")
    assert media_path(vods, "2", MediaName.source) is None


def test_byte_ranges():
    r = api_v2_media._byte_range
    assert r(None, 100) is None
    assert r("bytes=0-", 100) == (0, 99)
    assert r("bytes=10-19", 100) == (10, 19)
    assert r("bytes=90-200", 100) == (90, 99)
    assert r("bytes=-10", 100) == (90, 99)
    assert r("bytes=0-1,5-6", 100) is None  # several: the whole file
    with pytest.raises(ApiError) as e:
        r("bytes=100-", 100)
    assert e.value.status == 416 and e.value.headers == {"Content-Range": "bytes */100"}


def test_browser_names():
    assert media_lease.browser_of(FIREFOX) == "Firefox on Windows"
    edge = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/141.0 Safari/537.36 Edg/141.0"
    assert media_lease.browser_of(edge) == "Edge on macOS"
    phone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Safari/604.1"
    assert media_lease.browser_of(phone) == "Safari on iOS"
    assert media_lease.browser_of(None) == "A browser"


async def test_a_stream_stops_once_its_lease_is_lost(monkeypatch):
    answers = iter([True, False])

    async def holds(lease_id):
        return next(answers)

    monkeypatch.setattr(media_lease, "holds", holds)
    monkeypatch.setattr(api_v2_media, "RECHECK_S", 0)
    monkeypatch.setattr(api_v2_media, "CHUNK", 4)
    f = io.BytesIO(b"0123456789abcdef")
    got = [c async for c in api_v2_media._chunks(f, 2, 15, TAB_A)]
    assert got == [b"2345"]  # checked before each chunk: held, then lost
    assert f.closed


# ── The routes (dev DB) ──────────────────────────────────────────────────────


@pytest.fixture
async def media(admin, settings):  # noqa: F811 - the imported fixture
    async with get_sessionmaker()() as s:
        await s.execute(delete(MediaLease))
        await s.commit()
    folder = settings.vod_dir / VOD
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{VOD}.mp4").write_bytes(bytes(range(256)) * 4)
    yield admin
    async with get_sessionmaker()() as s:
        await s.execute(delete(MediaLease))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.action == "media.take_over"))
        await s.commit()


async def _claim(c, tab, take_over=False, ua=FIREFOX):
    body = {"lease_id": tab, "vod_id": VOD, "file": "source", "take_over": take_over}
    return await c.post("/api/v2/media/lease", json=body, headers=KEY | {"User-Agent": ua})


async def test_one_tab_holds_the_files_and_another_can_take_them_over(media):
    r = await media.get(f"/api/v2/vods/{VOD}/media", headers=KEY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["name"] for f in body["items"]] == ["source"]
    assert body["items"][0]["size"] == 1024 and body["lease"] is None

    r = await _claim(media, TAB_A)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["yours"] and first["browser"] == "Firefox on Windows" and first["vod_id"] == VOD
    assert (await _claim(media, TAB_A)).json()["acquired_at"] == first["acquired_at"]  # mine again: kept

    r = await _claim(media, TAB_B, ua="Mozilla/5.0 (X11; Linux x86_64) Chrome/141.0 Safari/537.36")
    assert r.status_code == 409 and r.json()["code"] == "media_in_use"
    assert r.json()["holder"]["browser"] == "Firefox on Windows"
    held = (await media.get(f"/api/v2/vods/{VOD}/media", params={"lease": TAB_B}, headers=KEY)).json()["lease"]
    assert held["browser"] == "Firefox on Windows" and not held["yours"]

    # Tab A streams, with a range.
    url = f"/api/v2/vods/{VOD}/media/source"
    r = await media.get(url, params={"lease": TAB_A}, headers=KEY | {"Range": "bytes=256-511"})
    assert r.status_code == 206 and r.headers["content-range"] == "bytes 256-511/1024"
    assert r.content == bytes(range(256))
    assert (await media.get(url, params={"lease": TAB_A}, headers=KEY)).content == bytes(range(256)) * 4
    r = await media.get(url, params={"lease": TAB_B}, headers=KEY)
    assert r.status_code == 410 and r.json()["code"] == "lease_lost"
    assert (await media.put(f"/api/v2/media/lease/{TAB_A}", headers=KEY)).status_code == 204

    # Tab B takes over: audited; tab A's heartbeat and stream are refused, with who has it.
    r = await _claim(media, TAB_B, take_over=True, ua="Mozilla/5.0 (X11; Linux x86_64) Chrome/141.0")
    assert r.status_code == 200 and r.json()["yours"] and r.json()["browser"] == "Chrome on Linux"
    async with get_sessionmaker()() as s:
        [row] = (await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.action == "media.take_over"))).all()
    assert row.target == f"vod:{VOD}" and row.before["browser"] == "Firefox on Windows"
    r = await media.put(f"/api/v2/media/lease/{TAB_A}", headers=KEY)
    assert r.status_code == 410 and r.json()["holder"]["browser"] == "Chrome on Linux"
    assert (await media.get(url, params={"lease": TAB_A}, headers=KEY)).status_code == 410

    # Releasing someone else's lease does nothing; releasing your own frees it.
    assert (await media.delete(f"/api/v2/media/lease/{TAB_A}", headers=KEY)).status_code == 204
    assert (await _claim(media, TAB_A)).status_code == 409
    assert (await media.delete(f"/api/v2/media/lease/{TAB_B}", headers=KEY)).status_code == 204
    assert (await _claim(media, TAB_A)).status_code == 200


async def test_a_stale_lease_is_free_and_files_are_named_not_pathed(media):
    assert (await _claim(media, TAB_A)).status_code == 200
    async with get_sessionmaker()() as s:
        lease = await s.get(MediaLease, media_lease.SCOPE)
        assert lease is not None
        lease.heartbeat_at = lease.heartbeat_at.replace(year=2000)
        await s.commit()
    assert (await _claim(media, TAB_B)).status_code == 200  # free: no take-over needed, nothing audited
    async with get_sessionmaker()() as s:
        assert not (await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.action == "media.take_over"))).all()
    r = await media.get(f"/api/v2/vods/{VOD}/media/..%2F..%2Fetc", params={"lease": TAB_B}, headers=KEY)
    assert r.status_code in (404, 422)
    r = await media.get(f"/api/v2/vods/{VOD}/media/censored", params={"lease": TAB_B}, headers=KEY)
    assert r.status_code == 404 and r.json()["code"] == "media_not_found"
    r = await media.post("/api/v2/media/lease", json={"lease_id": "short"}, headers=KEY)
    assert r.status_code == 422
