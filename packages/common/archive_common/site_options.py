"""The site's own options (vods.vexoul.net's /manage/home), as both services read them.

The worker's admin routes write them (archive_worker/site_options.py), archive-api serves them at
``/v1/site/options``. ``cards``: the homepage's cards, in order, each one card's settings with its own
``id`` (a short key; two cards may be the same otherwise): ``{"id", "type": "live"}`` (the stream while live,
else the newest ``vods`` VOD), ``{"id", "type": "vods", "tag": name, "count": 1 | 2}`` (the newest VOD, or
two, of a home category; ``vods`` is every VOD) or ``{"id", "type": "games"}`` (the most played games). A card
for a tag that is not a home category (site_tags.home_categories) is left out as it is read, so turning a
tag's page off takes its cards off the homepage (ADR 0005). ``playDefault``: what a synthetic VOD's watch
page plays first, ``"segments"`` (its sources' uploads, as composed) or ``"compiled"`` (its own uploads
from a compile job, where it has them; the page offers the other either way). An option never saved
reads as its default.

``homeCards`` is the cards in the shape before ``cards`` (``{"kind": "latest" | "one" | "tag" | "games",
"tag"?}``), served next to it until every site reads ``cards``; a row saved in that shape is read as ``cards``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from . import site_tags
from .models import SiteSetting

KEY = "options"  # the site_settings row
DEFAULTS: dict[str, Any] = {  # what the row keeps
    "cards": [
        {"id": "c0", "type": "live"},
        {"id": "c1", "type": "vods", "tag": "playthroughs", "count": 2},
        {"id": "c2", "type": "games"},
    ],
    "playDefault": "segments",
}
SERVED = ("homeCards", *DEFAULTS)  # what the API returns: the row's options, and the cards in the old shape

# Old card kind -> new card (without its id), and back.
_FROM_KIND: dict[str, dict[str, Any]] = {
    "latest": {"type": "live"},
    "one": {"type": "vods", "count": 1},
    "tag": {"type": "vods", "count": 2},
    "games": {"type": "games"},
}
LEGACY_KINDS = tuple(_FROM_KIND)


def from_legacy(card: dict[str, Any], card_id: str) -> dict[str, Any]:
    """A ``homeCards`` card (``kind`` already checked) as a ``cards`` one."""
    new = _FROM_KIND[card["kind"]]
    return {"id": card_id, **new, **({"tag": card["tag"]} if "count" in new else {})}


def legacy(card: dict[str, Any]) -> dict[str, Any]:
    """A ``cards`` card in the old ``homeCards`` shape."""
    if card["type"] == "vods":
        return {"kind": "one" if card["count"] == 1 else "tag", "tag": card["tag"]}
    return {"kind": "latest" if card["type"] == "live" else "games"}


async def load(conn: Any) -> dict[str, Any]:
    """``{**options, "updatedAt", "updatedBy"}`` (``updatedAt`` a datetime or None while never saved).
    ``conn``: an async connection or session."""
    row = (
        await conn.execute(
            select(SiteSetting.value, SiteSetting.updated_at, SiteSetting.updated_by).where(SiteSetting.key == KEY)
        )
    ).first()
    saved = dict(row.value) if row else {}
    if "cards" not in saved and "homeCards" in saved:  # saved before cards: ids by position, steady across reads
        saved["cards"] = [from_legacy(c, f"c{i}") for i, c in enumerate(saved["homeCards"])]
    options = {k: saved.get(k, v) for k, v in DEFAULTS.items()}
    tags = await site_tags.load(conn)
    if tags is not None:
        shown = site_tags.home_categories(tags["tags"]) | {site_tags.VODS}
        options["cards"] = [c for c in options["cards"] if "tag" not in c or c["tag"] in shown]
    return {
        "homeCards": [legacy(c) for c in options["cards"]],
        **options,
        "updatedAt": row.updated_at if row else None,
        "updatedBy": row.updated_by if row else None,
    }
