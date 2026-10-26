"""How each VOD tag shows on the site (vods.vexoul.net's /manage/tags), as both services read it.

The worker's admin routes write it (archive_worker/site_tags.py), archive-api serves it at
``/v1/site/tags``. A tag is ``{name, label, drawn, color, shape, width, height, rotate}``, then the text drawn
on it (``TEXT_FIELDS``), its pattern (``PATTERN_FIELDS``) and its type (``TYPE_FIELDS``: ``page``, the
slug of its own menu entry and page or null, ``pageLabel``, that page's title, and ``homeCard``, stored as
whether it has a page: a tag with a page is a "home category", which can also be a homepage card; ADR 0005);
``shape`` is not stored with the list
but made from ``site_tag_shapes``: the path of the tag's SVG relative to the public API, versioned by
its content's hash. A list saved before a field existed reads it as null.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import SiteSetting, SiteTagShape

KEY = "tags"  # the site_settings row
VODS = "vods"  # every real VOD, and a merge or split: what /vods lists (ADR 0005)
COMPILATION = "compilation"  # a synthetic VOD exactly while it keeps itself up to date (``synthetic.auto``)
AUTO_TAGS = ("new", "updated", COMPILATION, VODS)  # set by the site or the archive: every list keeps them
MAX_TAGS = 32
SHAPE_MAX_BYTES = 64 * 1024
TEXT_FIELDS = ("text", "textColor", "textSize", "textX", "textY", "textRotate")  # all null without text
PATTERN_FIELDS = ("pattern", "patternColor", "patternSize")  # all null without a pattern
TYPE_FIELDS = ("page", "pageLabel", "homeCard")  # page/pageLabel both null without a page
# stored, in this order
FIELDS = ("name", "label", "drawn", "color", "width", "height", "rotate", *TEXT_FIELDS, *PATTERN_FIELDS, *TYPE_FIELDS)


def shape_path(name: str, digest: str) -> str:
    return f"v1/site/tags/{name}.svg?v={digest}"


def with_shapes(tags: list[dict[str, Any]], hashes: dict[str, str]) -> list[dict[str, Any]]:
    """The stored tags as the API shows them: ``shape`` after ``color``, as the contract lists it."""
    out = []
    for tag in tags:
        digest = hashes.get(tag["name"])
        shown = {k: tag.get(k) for k in ("name", "label", "drawn", "color")}
        shown["shape"] = shape_path(tag["name"], digest) if digest else None
        shown.update({k: tag.get(k) for k in FIELDS[4:]})
        out.append(shown)
    return out


async def load(conn: Any) -> dict[str, Any] | None:
    """``{"tags", "updatedAt", "updatedBy"}`` (``updatedAt`` a datetime), or None while the list was
    never saved. ``conn``: an async connection or session."""
    row = (
        await conn.execute(
            select(SiteSetting.value, SiteSetting.updated_at, SiteSetting.updated_by).where(SiteSetting.key == KEY)
        )
    ).first()
    if row is None:
        return None
    hashes = dict((await conn.execute(select(SiteTagShape.name, SiteTagShape.hash))).all())
    return {
        "tags": with_shapes(list(row.value or []), hashes),
        "updatedAt": row.updated_at,
        "updatedBy": row.updated_by,
    }


def home_categories(tags: list[dict[str, Any]]) -> set[str]:
    """The tags with a page: the ones a homepage card can be for."""
    return {t["name"] for t in tags if t.get("page")}


async def shape(conn: Any, name: str) -> tuple[str, str] | None:
    """A tag's cleaned SVG and its hash, or None if it has no shape."""
    row = (await conn.execute(select(SiteTagShape.svg, SiteTagShape.hash).where(SiteTagShape.name == name))).first()
    return (row.svg, row.hash) if row else None
