"""The site's own options (the homepage's cards): the worker's admin routes save them, archive-api serves
them (the routes need the dev DB)."""

from typing import Any

import httpx
import pytest
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting
from archive_common.site_options import DEFAULTS, legacy
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.site_options import SiteOptionError, validate
from pydantic import SecretStr
from sqlalchemy import delete, func, select

KEY = {"Authorization": "Bearer k"}
LATEST, GAMES = {"kind": "latest"}, {"kind": "games"}
LIVE, GAMES_ = {"id": "l", "type": "live"}, {"id": "g", "type": "games"}


def card(tag: str) -> dict[str, Any]:
    return {"kind": "tag", "tag": tag}


def vods(card_id: str, tag: str, count: int = 2) -> dict[str, Any]:
    return {"id": card_id, "type": "vods", "tag": tag, "count": count}


TAGS: list[dict[str, Any]] = [
    {"name": "compilation", "page": "playthroughs"},
    {"name": "highlight", "page": "highlights"},
    {"name": "x", "homeCard": True},  # a card without a page: no longer a home category (ADR 0005)
]


def test_cards_kept_in_order():
    cards = [GAMES_, vods("a", "highlight"), {**LIVE, "extra": 1}, vods("b", "highlight"), vods("c", "vods", 1)]
    assert validate({"cards": cards}, TAGS) == {
        "cards": [GAMES_, vods("a", "highlight"), LIVE, vods("b", "highlight"), vods("c", "vods", 1)]
    }
    assert validate({"cards": []}, TAGS) == {"cards": []}
    twice = [LIVE, {**LIVE, "id": "l2"}, GAMES_, {**GAMES_, "id": "g2"}]
    assert validate({"cards": twice}, TAGS) == {"cards": twice}  # any card may repeat
    assert len(validate({"homeCards": [LATEST, LATEST]}, TAGS)["cards"]) == 2
    assert validate({"cards": [vods("a", "anything")]}, None) == {"cards": [vods("a", "anything")]}  # tags never saved


def test_old_shape_saved_as_cards():
    """``homeCards`` takes either shape; each old card gets an id."""
    out = validate({"homeCards": [LATEST, card("highlight"), {"kind": "one", "tag": "compilation"}, GAMES]}, TAGS)
    assert [{k: v for k, v in c.items() if k != "id"} for c in out["cards"]] == [
        {"type": "live"},
        {"type": "vods", "tag": "highlight", "count": 2},
        {"type": "vods", "tag": "compilation", "count": 1},
        {"type": "games"},
    ]
    assert len({c["id"] for c in out["cards"]}) == 4
    assert validate({"homeCards": [GAMES_, card("highlight")]}, TAGS)["cards"][0] == GAMES_
    assert validate({"cards": [{"type": "live"}]}, TAGS)["cards"][0]["id"]  # an id when sent without


def test_play_default():
    assert validate({"playDefault": "compiled"}, TAGS) == {"playDefault": "compiled"}


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        (None, "send an object"),
        ({}, "send an object"),
        ({"other": 1}, "send an object"),
        ({"cards": [], "homeCards": []}, "send an object"),
        ({"cards": {}}, "a list"),
        ({"homeCards": {}}, "a list"),
        ({"cards": [vods(str(i), "highlight") for i in range(13)]}, "a list"),
        ({"cards": [vods("a", "highlight"), vods("a", "compilation")]}, "already a card 'a'"),
        ({"cards": [{**LIVE, "id": "no spaces"}]}, "id must be"),
        ({"cards": [{**LIVE, "id": 3}]}, "id must be"),
        ({"cards": [vods("a", "x")]}, "not a tag that can be"),
        ({"homeCards": [card("gone")]}, "not a tag that can be"),
        ({"homeCards": [{"kind": "one"}]}, "not a tag that can be"),
        ({"cards": [vods("a", "highlight", 3)]}, "count must be"),
        ({"cards": [{**vods("a", "highlight"), "count": True}]}, "count must be"),
        ({"homeCards": [{"kind": "nope"}]}, "kind must be"),
        ({"cards": [{"type": "nope"}]}, "type must be"),
        ({"cards": ["live"]}, "type must be"),
        ({"playDefault": "youtube"}, "playDefault must be"),
    ],
)
def test_refused(body, msg):
    with pytest.raises(SiteOptionError, match=msg):
        validate(body, TAGS)


# ── The routes ────────────────────────────────────────────────────────────


async def _clear():
    async with get_sessionmaker()() as s:
        await s.execute(delete(SiteSetting).where(SiteSetting.key.in_(["options", "tags"])))
        await s.commit()


@pytest.fixture
async def clean(db):
    await _clear()
    async with get_sessionmaker()() as s:
        audit_after = (await s.execute(select(func.max(AUDIT_LOG.c.id)))).scalar() or 0
    yield audit_after
    await _clear()
    async with get_sessionmaker()() as s:
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.id > audit_after))
        await s.commit()


@pytest.fixture
def app(deps, clean):
    deps.settings.admin_api_key = SecretStr("k")
    return create_admin_app(deps, jobs.JobService.create(deps))  # never opened: nothing here runs a job


async def test_defaults_save_and_serve(app, api, clean):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert (await c.get("/admin/site/options")).status_code == 403
        r = await c.get("/admin/site/options", headers=KEY)
        old = [legacy(x) for x in DEFAULTS["cards"]]
        assert old == [LATEST, card("playthroughs"), GAMES]
        assert r.json() == {"homeCards": old, **DEFAULTS, "updatedAt": None, "updatedBy": None}
        cards = [GAMES_, vods("a", "vods", 1), vods("b", "vods", 1), LIVE]
        r = await c.put("/admin/site/options", headers=KEY, json={"cards": cards})
        assert r.status_code == 200 and r.json()["cards"] == cards and r.json()["updatedBy"] == "api-key"
        old = [GAMES, {"kind": "one", "tag": "vods"}, {"kind": "one", "tag": "vods"}, LATEST]
        assert r.json()["homeCards"] == old
        r = await c.put("/admin/site/options", headers=KEY, json={"cards": [LIVE, LIVE]})
        assert r.status_code == 400 and "already" in r.json()["msg"]
    async with api:
        served = {"homeCards": old, "cards": cards, "playDefault": "segments"}
        assert (await api.get("/v1/site/options")).json() == served
    async with get_sessionmaker()() as s:
        c_ = AUDIT_LOG.c
        rows = (await s.execute(select(AUDIT_LOG).where(c_.id > clean, c_.action == "site.options.update"))).all()
    assert [(r.target, r.after) for r in rows] == [("site:options", {"cards": cards, "playDefault": "segments"})]


async def test_row_saved_in_the_old_shape_reads_as_cards(app, clean):
    async with get_sessionmaker()() as s:
        s.add(SiteSetting(key="options", value={"homeCards": [card("highlight"), LATEST], "playDefault": "compiled"}))
        await s.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        got = (await c.get("/admin/site/options", headers=KEY)).json()
        assert got["cards"] == [vods("c0", "highlight"), {"id": "c1", "type": "live"}]
        assert got["homeCards"] == [card("highlight"), LATEST]
        r = await c.put("/admin/site/options", headers=KEY, json={"playDefault": "segments"})
        assert r.status_code == 200
    async with get_sessionmaker()() as s:
        row = await s.get(SiteSetting, "options")
    assert row is not None and row.value == {"cards": got["cards"], "playDefault": "segments"}  # now the new shape


async def test_card_of_a_tag_no_longer_a_home_category_goes(app, api, clean):
    """Taking a tag's page away takes its cards off the homepage, saved or default (ADR 0005)."""
    tag = {"label": "x", "drawn": False}
    tags = [
        {"name": n, **tag, "page": p, "pageLabel": p and p.title()}
        for n, p in [("new", None), ("updated", None), ("compilation", None), ("vods", "vods"), ("highlight", "hl")]
    ]
    cards = [LATEST, card("highlight"), {"kind": "one", "tag": "highlight"}, GAMES, {"kind": "one", "tag": "vods"}]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert (await c.put("/admin/site/tags", headers=KEY, json={"tags": tags})).status_code == 200
        assert (await c.put("/admin/site/options", headers=KEY, json={"homeCards": cards})).status_code == 200
        tags[-1]["page"] = None
        assert (await c.put("/admin/site/tags", headers=KEY, json={"tags": tags})).status_code == 200
        got = (await c.get("/admin/site/options", headers=KEY)).json()
        old = [LATEST, GAMES, {"kind": "one", "tag": "vods"}]  # vods (every VOD) is always a home category
        assert got["homeCards"] == old and [x["type"] for x in got["cards"]] == ["live", "games", "vods"]
    async with api:
        assert (await api.get("/v1/site/options")).json()["homeCards"] == old


async def test_v2_options_in_snake_case(app, clean):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        assert (await c.get("/api/v2/site/options")).status_code == 401
        got = (await c.get("/api/v2/site/options", headers=KEY)).json()
        assert got == {
            "cards": DEFAULTS["cards"],
            "play_default": DEFAULTS["playDefault"],
            "updated_at": None,
            "updated_by": None,
        }
        cards = [GAMES_, LIVE]
        r = await c.put("/api/v2/site/options", headers=KEY, json={"cards": cards, "play_default": "compiled"})
        assert r.status_code == 200, r.text
        assert r.json()["cards"] == cards and r.json()["play_default"] == "compiled" and "home_cards" not in r.json()
        assert r.json()["updated_at"].endswith("Z")
        r = await c.put("/api/v2/site/options", headers=KEY, json={"cards": [LIVE, LIVE]})
        assert r.status_code == 422 and r.json()["code"] == "invalid_site_options"
        r = await c.put("/api/v2/site/options", headers=KEY, json={"play_default": "nope"})
        assert r.json()["code"] == "invalid_site_options" and r.json()["detail"].startswith("play_default must be")
        r = await c.put("/api/v2/site/options", headers=KEY, json={})
        assert "play_default" in r.json()["detail"] and "playDefault" not in r.json()["detail"]
        for key in ("home_cards", "playDefault"):
            r = await c.put("/api/v2/site/options", headers=KEY, json={key: "segments"})
            assert r.status_code == 422 and r.json()["code"] == "invalid", key
            assert r.json()["errors"] == [
                {"loc": ["body", key], "msg": "Extra inputs are not permitted", "type": "extra_forbidden"}
            ]
        # /admin keeps its camelCase texts
        r = await c.put("/admin/site/options", headers=KEY, json={"playDefault": "nope"})
        assert r.status_code == 400 and r.json()["msg"].startswith("playDefault must be")
        assert (await c.get("/admin/site/options", headers=KEY)).json()["playDefault"] == "compiled"
    async with get_sessionmaker()() as s:
        c_ = AUDIT_LOG.c
        rows = (await s.execute(select(AUDIT_LOG).where(c_.id > clean, c_.action == "site.options.update"))).all()
    assert [(r.target, r.after) for r in rows] == [("site:options", {"cards": cards, "playDefault": "compiled"})]
    assert rows[0].request_id
