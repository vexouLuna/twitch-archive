"""Changing the site's own options (/admin/site/options; archive_common/site_options.py reads them).

PUT takes any of the options; the ones left out keep their value. All or nothing.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from archive_common import site_options, site_tags
from archive_common.db import get_sessionmaker
from archive_common.models import SiteSetting
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from .events import iso_utc

MAX_CARDS = 12
SINGLE = ("live", "games")  # cards there is one of
COUNTS = (1, 2)
PLAY = ("segments", "compiled")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class SiteOptionError(Exception):
    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg


def _card(i: int, card: Any, home_tags: set[str] | None) -> dict[str, Any]:
    """One card, either shape, as a ``cards`` one; an old-shape card, or a new one sent without an id, gets one."""
    if isinstance(card, dict) and "kind" in card and "type" not in card:
        if card["kind"] not in site_options.LEGACY_KINDS:
            raise SiteOptionError(f"card {i + 1}: kind must be latest, one, tag or games")
        card = site_options.from_legacy({"tag": None, **card}, secrets.token_urlsafe(6))
    kind = card.get("type") if isinstance(card, dict) else None
    if kind not in (*SINGLE, "vods"):
        raise SiteOptionError(f"card {i + 1}: type must be live, vods or games")
    card_id = card.get("id") or secrets.token_urlsafe(6)
    if not isinstance(card_id, str) or not _ID.match(card_id):
        raise SiteOptionError(f"card {i + 1}: id must be 1 to 32 letters, digits, - or _")
    if kind in SINGLE:
        return {"id": card_id, "type": kind}
    tag, count = card.get("tag"), card.get("count")
    if not isinstance(tag, str) or (home_tags is not None and tag not in home_tags | {site_tags.VODS}):
        raise SiteOptionError(f"card {i + 1}: {tag!r} is not a tag that can be a home card")
    if type(count) is not int or count not in COUNTS:
        raise SiteOptionError(f"card {i + 1}: count must be 1 or 2")
    return {"id": card_id, "type": kind, "tag": tag, "count": count}


def _cards(value: Any, home_tags: set[str] | None) -> list[dict[str, Any]]:
    """``home_tags``: the saved tags that are home categories, or None while the tag list was never saved.
    Cards may repeat (each has its own id), except ``live`` and ``games``."""
    if not isinstance(value, list) or len(value) > MAX_CARDS:
        raise SiteOptionError(f"cards must be a list of at most {MAX_CARDS} cards")
    out: list[dict[str, Any]] = []
    for i, card in enumerate(value):
        new = _card(i, card, home_tags)
        if any(c["id"] == new["id"] for c in out):
            raise SiteOptionError(f"card {i + 1}: there is already a card {new['id']!r}")
        if new["type"] in SINGLE and any(c["type"] == new["type"] for c in out):
            raise SiteOptionError(f"card {i + 1}: there is already a {new['type']!r} card")
        out.append(new)
    return out


def validate(body: Any, tags: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The options a PUT changes, checked. ``tags``: the saved tag list (site_tags.load), or None.
    The cards come as ``cards`` or, in either shape, as the old ``homeCards``; they are kept as ``cards``."""
    if (
        not isinstance(body, dict)
        or not body
        or set(body) - set(site_options.SERVED)
        or {"cards", "homeCards"} <= set(body)
    ):
        raise SiteOptionError(f"send an object with any of: {', '.join(site_options.DEFAULTS)}")
    home_tags = None if tags is None else site_tags.home_categories(tags)
    key = "cards" if "cards" in body else "homeCards"
    out = {"cards": _cards(body[key], home_tags)} if key in body else {}
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
