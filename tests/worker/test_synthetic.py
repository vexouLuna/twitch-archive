"""Synthetic VODs through /api/v2 and as archive-api serves them, against the dev DB.

A (2 h) and B (1 h) are one broadcast that Twitch cut in two: B started 7500 s after A, so a merge
has a 300 s gap. A plays Just Chatting then the test game for an hour; B plays the test game.
"""

import datetime as dt
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from archive_api.games_played import games_played
from archive_api.main import create_app
from archive_common.audit import AUDIT_LOG
from archive_common.config import get_settings
from archive_common.db import get_engine, get_sessionmaker
from archive_common.models import Job, Vod, VodSegment
from archive_worker import synthetic
from archive_worker.admin import create_admin_app
from archive_worker.vods import splice_reason
from pydantic import SecretStr
from sqlalchemy import delete, func, or_, select, update

A, B = "test-syn-a", "test-syn-b"
AB, P = f"{A}+{B}", "test-syn-p"
KEY = {"Authorization": "Bearer k"}
START = dt.datetime(2001, 3, 4, 20, 0, tzinfo=dt.UTC)  # before any real VOD
OFFSET = 7500
GAME = "test-syn-game"


def _ch(start, length, name="Just Chatting", game_id="509658"):
    return {
        "gameId": game_id,
        "name": name,
        "image": None,
        "duration": "00:00:00",
        "start": start,
        "end": length,
        "restricted": False,
    }


async def _clean():
    async with get_sessionmaker()() as s:
        mine = or_(Vod.id.like("test-syn-%"))
        synthetic_ids = select(Vod.id).where(mine, Vod.synthetic.is_not(None))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.like("vod:test-syn-%")))
        await s.execute(delete(Job).where(Job.vod_id.like("test-syn-%")))
        await s.execute(delete(VodSegment).where(VodSegment.vod_id.in_(synthetic_ids)))
        await s.execute(delete(Vod).where(mine, Vod.synthetic.is_not(None)))
        await s.execute(delete(Vod).where(mine))
        await s.commit()


@pytest.fixture
async def vods(db):
    await _clean()
    async with get_sessionmaker()() as s:
        s.add_all(
            [
                Vod(
                    id=A,
                    title="big stream",
                    created_at=START,
                    duration="02:00:00",
                    stream_id="2001",
                    chapters=[_ch(0, 3600), _ch(3600, 3600, "Test Game", GAME)],
                    thumbnail_url="https://t/a",
                    tags=["vods"],  # every real VOD (ADR 0005)
                ),
                Vod(
                    id=B,
                    title="big stream ",
                    created_at=START + dt.timedelta(seconds=OFFSET),
                    duration="01:00:00",
                    stream_id="2002",
                    chapters=[_ch(0, 3600, "Test Game", GAME)],
                    tags=["vods"],
                ),
            ]
        )
        await s.flush()
        await s.commit()
    yield
    await _clean()


@pytest.fixture
async def admin(vods, deps, make_service):
    deps.settings.admin_api_key = SecretStr("k")
    app = create_admin_app(deps, await make_service(start=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        yield c


async def public(path: str) -> httpx.Response:
    """archive-api, a fresh app each time (so nothing is served from its caches)."""
    settings = get_settings()
    settings.rate_limit_points = 1_000_000
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://t") as c:
        return await c.get(path)


async def _sources() -> dict[str, Any]:
    async with get_sessionmaker()() as s:
        return {
            v.id: {
                k: getattr(v, k)
                for k in (
                    "title",
                    "duration",
                    "chapters",
                    "youtube",
                    "drive",
                    "hidden",
                    "thumbnail_url",
                    "merged_into",
                    "created_at",
                    "tags",
                    "synthetic",
                )
            }
            for v in (await s.execute(select(Vod).where(Vod.id.in_((A, B))))).scalars()
        }


def _same(a: str, b: str) -> bool:
    """Two ISO times (archive-api's and the admin API's formats differ) are the same instant, to the ms."""
    t = [dt.datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b)]
    return abs(t[0] - t[1]) < dt.timedelta(milliseconds=1)


def _ids(page: dict[str, Any]) -> set[str]:
    return {v["id"] for v in page["data"]}


ALL = "&".join(f"id[$in][]={quote(i)}" for i in (A, B, AB, P, f"{A}-1", f"{A}-2"))


async def test_merge_split_and_undo(admin):
    before = await _sources()

    hidden = {k: {**v, "hidden": True} for k, v in before.items()}

    r = await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"source": B})  # an older dashboard's
    assert r.status_code == 201, r.text
    view = r.json()
    assert (view["id"], view["supersedes"], view["tags"], view["duration"]) == (AB, False, ["vods"], "03:05:00")
    assert view["segments"] == [
        {"vod_id": A, "start": 0, "end": None, "at": 0, "label": None},
        {"vod_id": B, "start": 0, "end": None, "at": OFFSET, "label": None},
    ]
    assert await _sources() == hidden  # the originals are only hidden (ADR 0005)

    vod = (await public(f"/vods/{AB}")).json()
    made = vod["synthetic"].pop("madeAt")
    assert made == vod["synthetic"].pop("changedAt") and _same(made, view["made_at"])
    assert vod["synthetic"] == {
        "supersedes": False,
        "firstLiveAt": "2001-03-04T20:00:00.000Z",
        "lastLiveAt": "2001-03-04T23:05:00.000Z",
        "segments": [
            {"vodId": A, "start": 0, "end": 7200, "at": 0, "label": None, "stream": 0},
            {"vodId": B, "start": 0, "end": 3600, "at": OFFSET, "label": None, "stream": 1},
        ],
    }
    assert (vod["duration"], vod["tags"], vod["createdAt"]) == ("03:05:00", ["vods"], "2001-03-04T20:00:00.000Z")
    assert [(c["start"], c.get("kind")) for c in vod["chapters"]] == [
        (0, None),
        (3600, None),
        (7200, "gap"),
        (OFFSET, None),
    ]

    # Hidden, and played by one shown synthetic VOD: still answered, and its links lead there.
    original = (await public(f"/vods/{A}")).json()
    assert original["superseded_by"] == [{"id": AB, "start": 0, "end": None, "at": 0}]
    assert original["hidden"] is True
    assert "synthetic" not in original and "appears_in" not in original
    assert _ids((await public(f"/vods?{ALL}")).json()) == {AB}
    assert _ids((await public(f"/vods?{ALL}&$superseded=true")).json()) == {AB}  # accepted, does nothing
    assert _ids((await public(f"/vods?{ALL}&$tag=vods")).json()) == {AB}

    page = (await public(f"/v1/vods/{quote(AB)}/comments?content_offset_seconds=0")).json()
    assert page["comments"] == [] and [s["vodId"] for s in page["segments"]] == [A, B]
    assert (await public(f"/v1/vods/{B}/comments?content_offset_seconds=0")).status_code != 404

    # Jobs: the originals are left alone, so they still run; the synthetic VOD is no Twitch VOD.
    assert await splice_reason(A) is None and await splice_reason(B) is None
    assert "synthetic" in await splice_reason(AB)  # type: ignore[operator]

    r = await admin.patch(f"/api/v2/vods/{quote(AB)}", headers=KEY, json={"duration": "01:00:00"})
    assert (r.status_code, r.json()["code"]) == (409, "vod_synthetic")
    detail = (await admin.get(f"/api/v2/vods/{A}", headers=KEY)).json()
    assert [x["id"] for x in detail["in_synthetic"]] == [AB]

    # Deleting it shows the VODs it hid again.
    r = await admin.delete(f"/api/v2/synthetic/{quote(AB)}", headers=KEY)
    assert r.status_code == 200 and r.json()["id"] == AB
    assert "superseded_by" not in (await public(f"/vods/{A}")).json()
    assert _ids((await public(f"/vods?{ALL}")).json()) == {A, B}
    assert await _sources() == before

    # A split, anywhere (no upload boundary needed).
    r = await admin.post(f"/api/v2/vods/{A}/split", headers=KEY, json={"at": 1000.5})
    assert r.status_code == 201, r.text
    assert [(v["id"], v["duration"]) for v in r.json()] == [(f"{A}-1", "00:16:41"), (f"{A}-2", "01:43:20")]
    second = (await public(f"/vods/{A}-2")).json()
    assert second["synthetic"]["segments"] == [
        {"vodId": A, "start": 1000.5, "end": 7200, "at": 0, "label": None, "stream": 0}
    ]
    assert second["createdAt"] == "2001-03-04T20:16:40.500Z"
    assert _ids((await public(f"/vods?{ALL}")).json()) == {f"{A}-1", f"{A}-2", B}
    # Hidden, in two shown synthetic VODs: answered with both, for the site to list.
    original = (await public(f"/vods/{A}")).json()
    assert original["hidden"] is True and "superseded_by" not in original
    assert [a["id"] for a in original["appears_in"]] == [f"{A}-1", f"{A}-2"]
    for half in (f"{A}-1", f"{A}-2"):
        assert (await admin.patch(f"/api/v2/vods/{half}", headers=KEY, json={"hidden": True})).status_code == 200
    assert (await public(f"/vods/{A}")).status_code == 404  # played by no shown one: as missing
    assert (await public(f"/v1/vods/{A}/comments?content_offset_seconds=0")).status_code == 404

    async with get_sessionmaker()() as s:
        audited = (
            await s.execute(
                select(AUDIT_LOG.c.action, AUDIT_LOG.c.target)
                .where(AUDIT_LOG.c.target.like("vod:test-syn-%"))
                .order_by(AUDIT_LOG.c.id)
            )
        ).all()
    assert [tuple(a) for a in audited][:4] == [
        ("synthetic.create", f"vod:{AB}"),
        ("synthetic.delete", f"vod:{AB}"),
        ("synthetic.create", f"vod:{A}-1"),
        ("synthetic.create", f"vod:{A}-2"),
    ]


async def test_merge_many_with_ids_and_candidates(admin):
    r = await admin.get(f"/api/v2/vods/{B}/merge-candidates?hours=24", headers=KEY)
    assert [(c["id"], c["gap"]) for c in r.json()["items"]] == [(A, 300)]  # ended 300 s before B started
    assert (await admin.get(f"/api/v2/vods/{B}/merge-candidates", headers=KEY)).json()["items"] == []
    r = await admin.post(f"/api/v2/vods/{B}/merge", headers=KEY, json={"sources": [A], "gaps": [0]})
    assert r.status_code == 201, r.text
    assert r.json()["id"] == f"{A}-m" and r.json()["duration"] == "03:00:00"  # in start order, A first
    assert (await admin.delete(f"/api/v2/synthetic/{A}-m", headers=KEY)).status_code == 200
    r = await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"sources": [B], "id": f"{A}-x"})
    assert r.status_code == 201 and r.json()["id"] == f"{A}-x"
    assert (await admin.delete(f"/api/v2/synthetic/{A}-x", headers=KEY)).status_code == 200
    r = await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"sources": [B], "gaps": [1, 2]})
    assert r.status_code == 422
    r = await admin.post(f"/api/v2/vods/{A}/split", headers=KEY, json={"at": 60, "ids": [f"{A}-a", f"{A}-b"]})
    assert [v["id"] for v in r.json()] == [f"{A}-a", f"{A}-b"]
    for half in (f"{A}-a", f"{A}-b"):
        assert (await admin.delete(f"/api/v2/synthetic/{half}", headers=KEY)).status_code == 200


async def test_playthrough_tags_and_recompose(admin):
    r = await admin.get(f"/api/v2/playthrough-candidates?game_id={GAME}", headers=KEY)
    assert r.status_code == 200
    windows = r.json()["items"]
    assert [(w["vod_id"], w["start"], w["end"]) for w in windows] == [(A, 3600, 7200), (B, 0, 3600)]

    body = {
        "id": P,
        "title": "Test Game, all of it",
        "tags": ["playthroughs", "compilation"],  # compilation is the archive's: not without auto
        "segments": [w["segment"] for w in windows],
    }
    assert (await admin.post("/api/v2/synthetic", headers=KEY, json={**body, "tags": ["nope"]})).status_code == 422
    r = await admin.post("/api/v2/synthetic", headers=KEY, json=body)
    assert r.status_code == 201, r.text
    assert (r.json()["supersedes"], r.json()["tags"], r.json()["duration"]) == (False, ["playthroughs"], "02:00:00")
    assert [s["at"] for s in r.json()["segments"]] == [0, 3600]
    assert (await admin.post("/api/v2/synthetic", headers=KEY, json=body)).status_code == 409

    # Untagged lists keep it out; its tab has it; its sources stay listed and link to it.
    assert _ids((await public(f"/vods?{ALL}")).json()) == {A, B}
    assert _ids((await public(f"/vods?{ALL}&$tag=playthroughs")).json()) == {P}
    assert _ids((await public(f"/vods?{ALL}&$tag=*")).json()) == {A, B, P}
    assert _ids((await public(f"/vods?{ALL}&tags=playthroughs")).json()) == {P}
    original = (await public(f"/vods/{B}")).json()
    assert original["appears_in"] == [{"id": P, "title": "Test Game, all of it", "tags": ["playthroughs"]}]
    assert "superseded_by" not in original
    async with get_engine().connect() as conn:
        [game] = [g for g in await games_played(conn) if g["gameId"] == GAME]
    assert game["vods"] == 2  # A and B, not the playthrough too

    # A source's change reaches it on the next sweep.
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Vod)
            .where(Vod.id == B)
            .values(chapters=[_ch(0, 1800, "Test Game", GAME), _ch(1800, 1800, "Other")], updated_at=func.now())
        )
        await s.commit()
    assert P in await synthetic.stale_ids(limit=1000)
    assert P in await synthetic.recompose_stale()
    assert P not in await synthetic.stale_ids(limit=1000)
    vod = (await public(f"/vods/{P}?")).json()
    assert [(c["start"], c["name"]) for c in vod["chapters"]] == [
        (0, "Test Game"),
        (3600, "Test Game"),
        (5400, "Other"),
    ]

    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"tags": [], "title": "renamed"})
    assert r.status_code == 200 and (r.json()["tags"], r.json()["title"]) == ([], "renamed")
    assert _ids((await public(f"/vods?{ALL}")).json()) == {A, B, P}
    r = await admin.patch(f"/api/v2/vods/{P}", headers=KEY, json={"tags": ["compilation"], "hidden": True})
    assert r.status_code == 200, r.text
    assert (await public(f"/vods/{P}")).status_code == 404
    assert "appears_in" not in (await public(f"/vods/{B}")).json()  # a hidden VOD is linked from nowhere


async def _windows() -> list[tuple[str, float, float]]:
    async with get_sessionmaker()() as s:
        return [(vod.id, a, b) for vod, a, b in await synthetic.game_windows(s, GAME)]


async def test_auto_playthrough_sources(admin):
    """An auto playthrough plays the shown ``vods`` VODs: a merge rather than its hidden sources, a shown
    source of it not as well, and never a compilation (even one tagged ``vods``)."""
    # Twitch's last chapter runs past the whole-second duration: the window stops at the VOD's end,
    # so the next one follows it without a gap ("Technical difficulties") in between.
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Vod).where(Vod.id == A).values(chapters=[_ch(0, 3600), _ch(3600, 3600.6, "Test Game", GAME)])
        )
        await s.commit()
    assert await _windows() == [(A, 3600, 7200), (B, 0, 3600)]
    r = await admin.post("/api/v2/synthetic", headers=KEY, json={"id": P, "auto": {"game_id": GAME}})
    assert r.status_code == 201, r.text
    assert [x["at"] for x in r.json()["segments"]] == [0, 3600]
    assert not [c for c in (await public(f"/vods/{P}")).json()["chapters"] if c.get("restricted")]
    assert (await admin.delete(f"/api/v2/synthetic/{P}", headers=KEY)).status_code == 200

    r = await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"source": B})
    assert r.status_code == 201, r.text
    assert await _windows() == [(AB, 3600, 7200), (AB, 7500, 11100)]  # not the hidden A and B; not the gap
    async with get_sessionmaker()() as s:  # B shown again but still in the merge: counted once, as AB
        await s.execute(update(Vod).where(Vod.id == B).values(hidden=False))
        await s.commit()
    assert await _windows() == [(AB, 3600, 7200), (AB, 7500, 11100)]

    r = await admin.post("/api/v2/synthetic", headers=KEY, json={"id": P, "auto": {"game_id": GAME}})
    assert r.status_code == 201, r.text
    assert [(x["vod_id"], x["start"], x["end"], x["at"]) for x in r.json()["segments"]] == [
        (AB, 3600, 7200, 0),
        (AB, 7500, 11100, 3600),
    ]
    assert not [c for c in (await public(f"/vods/{P}")).json()["chapters"] if c.get("restricted")]
    r = await admin.patch(f"/api/v2/vods/{P}", headers=KEY, json={"tags": ["compilation", "vods"]})
    assert r.status_code == 200, r.text
    assert await _windows() == [(AB, 3600, 7200), (AB, 7500, 11100)]  # P is no source, and doesn't hide AB
    assert P not in await synthetic.refresh_auto()


async def test_nested_synthetic(admin):
    r = await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"source": B})
    assert r.status_code == 201, r.text
    # The test game, from the merged broadcast: A's second hour, the merge's gap, then B.
    body = {"id": P, "title": "Test Game", "tags": ["playthroughs"], "segments": [{"vod_id": AB, "start": 3600}]}
    r = await admin.post("/api/v2/synthetic", headers=KEY, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["segments"] == [{"vod_id": AB, "start": 3600, "end": None, "at": 0, "label": None}]

    # Served as windows of the real VODs (hidden by the merge, still played: ADR 0005).
    vod = (await public(f"/vods/{P}")).json()
    assert vod["synthetic"]["segments"] == [
        {"vodId": A, "start": 3600, "end": 7200, "at": 0, "label": None, "stream": 0},
        {"vodId": B, "start": 0, "end": 3600, "at": 3900, "label": None, "stream": 1},
    ]
    page = (await public(f"/v1/vods/{P}/comments?content_offset_seconds=0")).json()
    assert [s["vodId"] for s in page["segments"]] == [A, B]
    assert (await public(f"/vods/{quote(AB)}")).json()["appears_in"] == [
        {"id": P, "title": "Test Game", "tags": ["playthroughs"]}
    ]

    # No cycles, and the merge cannot go while the playthrough is made of it.
    r = await admin.put(f"/api/v2/synthetic/{quote(AB)}", headers=KEY, json={"segments": [{"vod_id": P}]})
    assert r.status_code == 422 and "made of itself" in r.text
    r = await admin.delete(f"/api/v2/synthetic/{quote(AB)}", headers=KEY)
    assert r.status_code == 409 and P in r.text

    # A change to a real VOD reaches the merge, then the playthrough, in one sweep.
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Vod)
            .where(Vod.id == B)
            .values(chapters=[_ch(0, 1800, "Test Game", GAME), _ch(1800, 1800, "Other")], updated_at=func.now())
        )
        await s.commit()
    done = await synthetic.recompose_stale()
    assert done.index(AB) < done.index(P)
    vod = (await public(f"/vods/{P}")).json()
    assert [c["name"] for c in vod["chapters"] if c.get("kind") != "gap"][-1] == "Other"

    assert (await admin.delete(f"/api/v2/synthetic/{P}", headers=KEY)).status_code == 200
    assert (await admin.delete(f"/api/v2/synthetic/{quote(AB)}", headers=KEY)).status_code == 200


async def test_changed_at(admin):
    """``changedAt`` moves when what it plays changes (segments, or a source growing), not on a rename or a
    source's chapter edit; ``lastLiveAt`` follows the latest footage."""

    async def stamps():
        syn = (await public(f"/vods/{P}")).json()["synthetic"]
        return syn["madeAt"], syn["changedAt"], syn["lastLiveAt"]

    r = await admin.post(
        "/api/v2/synthetic",
        headers=KEY,
        json={"id": P, "tags": ["compilation"], "segments": [{"vod_id": A, "start": 3600, "end": 5400}]},
    )
    assert r.status_code == 201, r.text
    made, changed, last = await stamps()
    assert made == changed and last == "2001-03-04T21:30:00.000Z"

    async with get_sessionmaker()() as s:  # a source's chapter edit, recomposed: same length
        await s.execute(
            update(Vod)
            .where(Vod.id == A)
            .values(chapters=[_ch(0, 3600), _ch(3600, 3600, "Renamed", GAME)], updated_at=func.now())
        )
        await s.commit()
    assert P in await synthetic.recompose_stale()
    assert (await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"title": "renamed"})).status_code == 200
    assert await stamps() == (made, changed, last)

    # Played again on a later stream: its window goes on the end.
    r = await admin.put(
        f"/api/v2/synthetic/{P}",
        headers=KEY,
        json={"segments": [{"vod_id": A, "start": 3600, "end": 5400}, {"vod_id": B, "start": 0, "end": 1800}]},
    )
    assert r.status_code == 200, r.text
    _, changed2, last2 = await stamps()
    assert changed2 > changed and _same(r.json()["changed_at"], changed2) and _same(r.json()["made_at"], made)
    assert last2 == "2001-03-04T22:35:00.000Z"

    # A source growing under an open-ended window lengthens it too.
    assert (
        await admin.put(
            f"/api/v2/synthetic/{P}",
            headers=KEY,
            json={"segments": [{"vod_id": A, "start": 3600, "end": 5400}, {"vod_id": B, "start": 0}]},
        )
    ).status_code == 200
    _, changed3, _ = await stamps()
    async with get_sessionmaker()() as s:
        await s.execute(update(Vod).where(Vod.id == B).values(duration="01:30:00", updated_at=func.now()))
        await s.commit()
    assert P in await synthetic.recompose_stale()
    _, changed4, last4 = await stamps()
    assert changed4 > changed3 and last4 == "2001-03-04T23:35:00.000Z"


async def test_live_span_filters(admin):
    """``firstLiveAt``/``lastLiveAt``: a synthetic VOD's span, a real VOD's ``createdAt``, filtered in the
    query so totals and paging hold. A starts 20:00, B 22:05; their merge AB spans 20:00-23:05."""
    assert (await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"source": B})).status_code == 201
    for v in (A, B):  # a merge hides its sources; shown again, they list beside it
        assert (await admin.patch(f"/api/v2/vods/{v}", headers=KEY, json={"hidden": False})).status_code == 200

    async def live(q: str) -> dict[str, Any]:
        r = await public(f"/vods?{ALL}&$superseded=true&{q}")
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    assert _ids(await live("lastLiveAt[$gte]=2001-03-04T23:00:00Z")) == {AB}
    assert _ids(await live("firstLiveAt[$gte]=2001-03-04T20:00:00Z&firstLiveAt[$lt]=2001-03-04T20:00:01Z")) == {A, AB}
    assert _ids(await live("firstLiveAt[$gt]=2001-03-04T20:00:00Z")) == {B}
    assert _ids(await live("firstLiveAt[$lt]=2001-03-04T21:00:00Z&lastLiveAt[$gt]=2001-03-04T23:00:00Z")) == {AB}
    assert _ids(await live("lastLiveAt[$lte]=2001-03-04T23:05:00.000Z")) == {A, B, AB}
    assert _ids(await live("lastLiveAt[$lt]=2001-03-04T20:00:00")) == set()  # naive is UTC

    # With the other filters, and paged: the total counts what matched, not the page.
    assert _ids(await live("firstLiveAt[$lt]=2001-03-04T21:00:00Z&$tag=*")) == {A, AB}
    assert _ids(await live("firstLiveAt[$lt]=2001-03-04T21:00:00Z&createdAt[$gt]=2001-03-04T19:00:00Z")) == {A, AB}
    page = await live("lastLiveAt[$gte]=2001-03-04T22:00:00Z&$limit=1&$sort[createdAt]=-1")
    assert (page["total"], len(page["data"]), page["data"][0]["id"]) == (2, 1, B)
    page = await live("lastLiveAt[$gte]=2001-03-04T22:00:00Z&$limit=1&$skip=1&$sort[createdAt]=-1")
    assert (page["total"], [v["id"] for v in page["data"]]) == (2, [AB])

    for q in (
        "firstLiveAt=2001-03-04",
        "firstLiveAt[$ne]=2001-03-04",
        "lastLiveAt[$gte]=yesterday",
        "lastLiveAt[$gte][]=2001-03-04",
    ):
        r = await public(f"/vods?{ALL}&{q}")
        assert r.status_code == 400, (q, r.text)


def test_live_span_without_a_start():
    """A synthetic VOD none of whose sources has a start has no span, so no live filter matches it."""
    from archive_api.services import _live_filter
    from archive_common.serialize import Segment, live_span  # type: ignore[attr-defined]

    seg = Segment(source_id=A, start=0, end=60, at=0, label=None)
    assert live_span([seg], {A: None}) is None and live_span([], {}) is None
    clause = _live_filter("firstLiveAt", 0, {AB: None, P: (START, START)})({"$gte": "2001-01-01"})
    assert P in str(clause.compile(compile_kwargs={"literal_binds": True})) and AB not in str(
        clause.compile(compile_kwargs={"literal_binds": True})
    )


async def test_auto_playthrough(admin):
    bad = await admin.post("/api/v2/synthetic", headers=KEY, json={"id": P, "auto": {"game_id": "test-syn-none"}})
    assert bad.status_code == 422
    r = await admin.post(
        "/api/v2/synthetic", headers=KEY, json={"id": P, "tags": ["compilation"], "auto": {"game_id": GAME}}
    )
    assert r.status_code == 201, r.text
    assert r.json()["auto"] == {"game_id": GAME} and r.json()["tags"] == ["compilation"]
    assert [(x["vod_id"], x["start"], x["end"], x["at"]) for x in r.json()["segments"]] == [
        (A, 3600, 7200, 0),
        (B, 0, 3600, 3600),
    ]
    assert P not in await synthetic.refresh_auto()  # nothing changed

    # B plays less of the game now: the next round follows, audited as the system.
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Vod)
            .where(Vod.id == B)
            .values(chapters=[_ch(0, 1800, "Test Game", GAME), _ch(1800, 1800, "Other")], updated_at=func.now())
        )
        await s.commit()
    assert P in await synthetic.refresh_auto()
    view = (await admin.get(f"/api/v2/synthetic/{P}", headers=KEY)).json()
    assert [(x["vod_id"], x["end"]) for x in view["segments"]] == [(A, 7200), (B, 1800)]
    assert view["duration"] == "01:30:00"
    async with get_sessionmaker()() as s:
        [row] = (
            await s.execute(
                select(AUDIT_LOG).where(AUDIT_LOG.c.target == f"vod:{P}", AUDIT_LOG.c.action == "synthetic.update")
            )
        ).all()
    assert (row.actor_kind, row.detail) == ("system", {"auto": {"gameId": GAME}})

    # Other changes keep it; segments by hand stop it; it can follow again, or stop outright.
    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"title": "renamed"})
    assert r.json()["auto"] == {"game_id": GAME}
    r = await admin.put(
        f"/api/v2/synthetic/{P}", headers=KEY, json={"segments": [{"vod_id": B, "start": 0, "end": 600}]}
    )
    assert r.status_code == 200 and r.json()["auto"] is None and r.json()["tags"] == []
    assert P not in await synthetic.refresh_auto()
    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"auto": {"game_id": GAME}})
    assert r.json()["auto"] == {"game_id": GAME} and len(r.json()["segments"]) == 2
    assert r.json()["tags"] == ["compilation"]
    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"auto": None})
    assert r.json()["auto"] is None and len(r.json()["segments"]) == 2


async def test_compile_sources(admin, make_ctx):
    from archive_worker.context import StepRefused
    from archive_worker.steps import compile as comp

    async def sources(vod_id):
        ctx = make_ctx("compile", vod_id, {"type": "compiled"})
        await comp.compile_sources(ctx)
        return ctx.payload["clips"]

    with pytest.raises(StepRefused, match="not a synthetic"):
        await sources(A)
    r = await admin.post("/api/v2/synthetic", headers=KEY, json={"id": P, "auto": {"game_id": GAME}})
    assert r.status_code == 201, r.text
    with pytest.raises(StepRefused, match=f"no kept MP4 of source.s. {A}, {B}"):
        await sources(P)
    vod_dir = make_ctx().settings.vod_dir
    for v in (A, B):
        (vod_dir / v).mkdir(parents=True)
        (vod_dir / v / f"{v}.mp4").touch()
    assert [(c["vod_id"], c["start"], c["end"]) for c in await sources(P)] == [(A, 3600, 7200), (B, 0, 3600)]

    # A source's DMCA claims: listed for Manage's warning, split by whether a window it plays has them, and
    # the compile needs them accepted, any of them.
    def claim(start, length):
        return {
            "type": "CLAIM_TYPE_AUDIO",
            "claimPolicy": {"primaryPolicy": {"policyType": "POLICY_TYPE_BLOCK"}},
            "matchDetails": {"longestMatchStartTimeSeconds": start, "longestMatchDurationSeconds": length},
        }

    async def add_job(vod_id, kind, payload):
        async with get_sessionmaker()() as s:
            s.add(Job(kind=kind, vod_id=vod_id, state="done", payload=payload))
            await s.commit()

    async def dmca():
        r = await admin.get(f"/api/v2/synthetic/{P}/dmca", headers=KEY)
        assert r.status_code == 200, r.text
        return r.json()

    # B plays 0-3600; its part 2 starts at 3600, so a claim 10-30 s into it is outside.
    parts = [{"number": 2, "start": 3600, "end": 7200, "path": "x"}]
    await add_job(B, "part_dmca", {"claims": [claim(10, 20)], "start_part": 2, "parts": parts})
    b_other = {"vod_id": B, "part": 2, "mute": [[10, 30]], "blackout": [], "placed": True}
    assert await dmca() == {"overlapping": [], "other": [b_other]}
    # A plays 3600-7200: of its whole-VOD claims, 4000-4020 is in it and 100-120 isn't.
    await add_job(A, "dmca", {"claims": [claim(4000, 20), claim(100, 20)]})
    assert await dmca() == {
        "overlapping": [{"vod_id": A, "part": None, "mute": [[4000, 4020]], "blackout": [], "placed": True}],
        "other": [b_other, {"vod_id": A, "part": None, "mute": [[100, 120]], "blackout": [], "placed": True}],
    }
    with pytest.raises(StepRefused, match=f"{A}, {B} have DMCA claims"):
        await sources(P)
    ctx = make_ctx("compile", P, {"type": "compiled", "accept_dmca": True})
    await comp.compile_sources(ctx)
    assert len(ctx.payload["clips"]) == 2
    async with get_sessionmaker()() as s:
        await s.execute(delete(Job).where(Job.vod_id.in_((A, B))))
        await s.commit()

    # The merge has a gap: its windows still compile, back to back, each with where it plays (the gap
    # becomes its uploads' ``gaps``). A censored source's kept MP4 isn't what its uploads show.
    assert (await admin.post(f"/api/v2/vods/{A}/merge", headers=KEY, json={"source": B})).status_code == 201
    first, second = await sources(AB)
    assert second["at"] > first["at"] + first["end"] - first["start"] + 1
    async with get_sessionmaker()() as s:
        await s.execute(update(Vod).where(Vod.id == B).values(censor=[{"start": 0, "end": 5, "mode": "blur"}]))
        await s.commit()
    with pytest.raises(StepRefused, match=f"{B} have censor ranges"):
        await sources(P)
    assert (await admin.get("/api/v2/synthetic/test-syn-none/dmca", headers=KEY)).status_code == 404


async def test_compiled_uploads_go_outdated_when_the_segments_change(admin, make_ctx):
    from archive_worker.steps import compile as comp

    r = await admin.post("/api/v2/synthetic", headers=KEY, json={"id": P, "auto": {"game_id": GAME}})
    assert r.status_code == 201 and r.json()["compiled"] is None  # nothing compiled yet
    async with get_sessionmaker()() as s:
        await s.execute(update(Vod).where(Vod.id == P).values(youtube=[{"id": "yt", "type": "compiled", "part": 1}]))
        await s.commit()
        made = (await s.execute(select(Vod.synthetic).where(Vod.id == P))).scalar_one()["changedAt"]
    ctx = make_ctx("compile", P, {"type": "compiled", "uploaded": {"1": {}}, "compiled_from": made})
    await comp.compiled(ctx)
    assert (await admin.get(f"/api/v2/synthetic/{P}", headers=KEY)).json()["compiled"] == {"outdated": False}
    r = await admin.put(
        f"/api/v2/synthetic/{P}", headers=KEY, json={"segments": [{"vod_id": B, "start": 0, "end": 600}]}
    )
    assert r.json()["compiled"] == {"outdated": True}


async def test_own_thumbnail(admin):
    """A synthetic VOD's own thumbnail wins over its first source's, through recomposes and new segments,
    until it is set back to null."""
    a, b = {"vod_id": A, "start": 3600, "end": 7200}, {"vod_id": B}
    r = await admin.post("/api/v2/synthetic", headers=KEY, json={"id": P, "segments": [a, b]})
    assert r.status_code == 201, r.text

    async def thumb() -> str | None:
        return (await admin.get(f"/api/v2/vods/{P}", headers=KEY)).json()["thumbnail_url"]  # type: ignore[no-any-return]

    assert await thumb() == "https://t/a"
    r = await admin.patch(f"/api/v2/vods/{P}", headers=KEY, json={"thumbnail_url": "https://t/own"})
    assert r.status_code == 200 and r.json()["thumbnail_url"] == "https://t/own", r.text
    assert r.json()["synthetic"]["thumbnailUrl"] == "https://t/own"
    assert P in await synthetic.recompose(P)
    assert await thumb() == "https://t/own"
    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"segments": [b]})
    assert r.status_code == 200, r.text
    assert await thumb() == "https://t/own"
    assert (await public(f"/vods/{P}")).json()["thumbnail_url"] == "https://t/own"

    r = await admin.patch(f"/api/v2/vods/{P}", headers=KEY, json={"thumbnail_url": None})
    assert r.status_code == 200 and r.json()["thumbnail_url"] is None  # B, its one source, has none
    assert "thumbnailUrl" not in r.json()["synthetic"]
    r = await admin.put(f"/api/v2/synthetic/{P}", headers=KEY, json={"segments": [a, b]})
    assert await thumb() == "https://t/a"
