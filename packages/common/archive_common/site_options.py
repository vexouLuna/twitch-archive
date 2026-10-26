"""The site's own options (vods.vexoul.net's /manage/home), as both services read them.

The worker's admin routes write them (archive_worker/site_options.py), archive-api serves them at
``/v1/site/options``. ``homeCards``: the homepage's cards, in order, each ``{"kind": "latest"}`` (the newest
``vods`` VOD, or the stream while live), ``{"kind": "one", "tag": name}`` (the newest VOD of a home category,
never the stream), ``{"kind": "tag", "tag": name}`` (its two newest) or ``{"kind": "games"}`` (the most
played games). A card for a tag that is not a home category (site_tags.home_categories) is left out as it
is read, so turning a tag's page off takes its cards off the homepage (ADR 0005). ``playDefault``: what a
synthetic VOD's watch page plays first, ``"segments"`` (its sources' uploads, as composed) or ``"compiled"``
(its own uploads from a compile job, where it has them; the page offers the other either way). An option never saved
reads as its default.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from . import site_tags
from .models import SiteSetting

KEY = "options"  # the site_settings row
DEFAULTS: dict[str, Any] = {
    "homeCards": [{"kind": "latest"}, {"kind": "tag", "tag": "playthroughs"}, {"kind": "games"}],
    "playDefault": "segments",
}


async def load(conn: Any) -> dict[str, Any]:
    """``{**options, "updatedAt", "updatedBy"}`` (``updatedAt`` a datetime or None while never saved).
    ``conn``: an async connection or session."""
    row = (
        await conn.execute(
            select(SiteSetting.value, SiteSetting.updated_at, SiteSetting.updated_by).where(SiteSetting.key == KEY)
        )
    ).first()
    saved = row.value if row else {}
    options = {k: saved.get(k, v) for k, v in DEFAULTS.items()}
    tags = await site_tags.load(conn)
    if tags is not None:
        shown = site_tags.home_categories(tags["tags"])
        options["homeCards"] = [c for c in options["homeCards"] if "tag" not in c or c["tag"] in shown]
    return {
        **options,
        "updatedAt": row.updated_at if row else None,
        "updatedBy": row.updated_by if row else None,
    }
