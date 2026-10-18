"""The site's own options (the homepage's cards): the worker's admin routes save them, archive-api serves
them (the routes need the dev DB)."""

from typing import Any

import httpx
import pytest
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting
from archive_common.site_options import DEFAULTS
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.site_options import SiteOptionError, validate
from pydantic import SecretStr
from sqlalchemy import delete, func, select

KEY = {"Authorization": "Bearer k"}
LATEST, GAMES = {"kind": "latest"}, {"kind": "games"}


def card(tag: str) -> dict[str, Any]:
    return {"kind": "tag", "tag": tag}


TAGS: list[dict[str, Any]] = [{"name": "compilation", "homeCard": True}, {"name": "highlight", "homeCard": True}, {"name": "x"}]


def test_cards_kept_in_order():
    cards = [GAMES, card("highlight"), {**LATEST, "extra": 1}, card("compilation")]
    assert validate({"homeCards": cards}, TAGS) == {
        "homeCards": [GAMES, card("highlight"), LATEST, card("compilation")]
    }
    assert validate({"homeCards": []}, TAGS) == {"homeCards": []}
    assert validate({"homeCards": [card("anything")]}, None) == {"homeCards": [card("anything")]}  # tags never saved


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        (None, "send an object"),
        ({}, "send an object"),
        ({"other": 1}, "send an object"),
        ({"homeCards": {}}, "a list"),
        ({"homeCards": [LATEST] * 13}, "a list"),
        ({"homeCards": [LATEST, LATEST]}, "already a 'latest'"),
        ({"homeCards": [card("x")]}, "not a tag that can be"),
        ({"homeCards": [card("gone")]}, "not a tag that can be"),
        ({"homeCards": [card("highlight"), card("highlight")]}, "already a card for"),
        ({"homeCards": [{"kind": "nope"}]}, "kind must be"),
        ({"homeCards": ["latest"]}, "kind must be"),
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
        assert r.json() == {**DEFAULTS, "updatedAt": None, "updatedBy": None}
        cards = [GAMES, LATEST]
        r = await c.put("/admin/site/options", headers=KEY, json={"homeCards": cards})
        assert r.status_code == 200 and r.json()["homeCards"] == cards and r.json()["updatedBy"] == "api-key"
        r = await c.put("/admin/site/options", headers=KEY, json={"homeCards": [LATEST, LATEST]})
        assert r.status_code == 400 and "already" in r.json()["msg"]
    async with api:
        assert (await api.get("/v1/site/options")).json() == {"homeCards": cards}
    async with get_sessionmaker()() as s:
        c_ = AUDIT_LOG.c
        rows = (await s.execute(select(AUDIT_LOG).where(c_.id > clean, c_.action == "site.options.update"))).all()
    assert [(r.target, r.after) for r in rows] == [("site:options", {"homeCards": cards})]
