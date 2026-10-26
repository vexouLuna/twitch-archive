"""Changing the site's own options (/admin/site/options; archive_common/site_options.py reads them).

PUT takes any of the options; the ones left out keep their value. All or nothing.
"""

from __future__ import annotations

from typing import Any

from archive_common import site_options, site_tags
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from .events import iso_utc

MAX_CARDS = 12
SINGLE = ("latest", "games")  # cards there is one of
PLAY = ("segments", "compiled")


class SiteOptionError(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg


TAGGED = ("one", "tag")  # cards for a home category: its newest VOD, its two newest


def _cards(value: Any, home_tags: set[str] | None) -> list[dict[str, Any]]:
    """``home_tags``: the saved tags that are home categories, or None while the tag list was never saved."""
    if not isinstance(value, list) or len(value) > MAX_CARDS:
        raise SiteOptionError(f"homeCards must be a list of at most {MAX_CARDS} cards")
    out: list[dict[str, Any]] = []
    for i, card in enumerate(value):
        kind = card.get("kind") if isinstance(card, dict) else None
        if kind in SINGLE:
            if {"kind": kind} in out:
                raise SiteOptionError(f"card {i + 1}: there is already a {kind!r} card")
            out.append({"kind": kind})
        elif kind in TAGGED:
            tag = card.get("tag")
            if not isinstance(tag, str) or (home_tags is not None and tag not in home_tags):
                raise SiteOptionError(f"card {i + 1}: {tag!r} is not a tag that can be a home card")
            if {"kind": kind, "tag": tag} in out:
                raise SiteOptionError(f"card {i + 1}: there is already a {kind!r} card for {tag!r}")
            out.append({"kind": kind, "tag": tag})
        else:
            raise SiteOptionError(f"card {i + 1}: kind must be latest, one, tag or games")
    return out


def validate(body: Any, tags: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The options a PUT changes, checked. ``tags``: the saved tag list (site_tags.load), or None."""
    if not isinstance(body, dict) or not body or set(body) - set(site_options.DEFAULTS):
        raise SiteOptionError(f"send an object with any of: {', '.join(site_options.DEFAULTS)}")
    home_tags = None if tags is None else site_tags.home_categories(tags)
    out = {"homeCards": _cards(body["homeCards"], home_tags)} if "homeCards" in body else {}
    if "playDefault" in body:
        if body["playDefault"] not in PLAY:
            raise SiteOptionError(f"playDefault must be one of: {', '.join(PLAY)}")
        out["playDefault"] = body["playDefault"]
    return out


def view(loaded: dict[str, Any]) -> dict[str, Any]:
    return {**loaded, "updatedAt": iso_utc(loaded["updatedAt"])}


def _options(loaded: dict[str, Any]) -> dict[str, Any]:
    return {k: loaded[k] for k in site_options.DEFAULTS}


async def save(body: Any, updated_by: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Change the options in ``body``. (before, after) options for the audit, and the new view."""
    async with get_sessionmaker()() as s:
        await s.execute(select(SiteSetting.key).where(SiteSetting.key == site_options.KEY).with_for_update())
        tags = await site_tags.load(s)
        changes = validate(body, tags["tags"] if tags else None)
        before = _options(await site_options.load(s))
        stmt = insert(SiteSetting).values(key=site_options.KEY, value={**before, **changes}, updated_by=updated_by)
        await s.execute(
            stmt.on_conflict_do_update(
                index_elements=[SiteSetting.key],
                set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by, "updated_at": func.now()},
            )
        )
        after = await site_options.load(s)
        await s.commit()
    return before, _options(after), view(after)
