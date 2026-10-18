"""The site's own options (vods.vexoul.net's /manage/home), as both services read them.

The worker's admin routes write them (archive_worker/site_options.py), archive-api serves them at
``/v1/site/options``. ``homeCards``: the homepage's cards, in order, each ``{"kind": "latest"}`` (the newest
VOD, or the stream while live), ``{"kind": "tag", "tag": name}`` (the newest VODs with a tag whose type has
``homeCard``; see site_tags) or ``{"kind": "games"}`` (the most played games). An option never saved
reads as its default.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from .models import SiteSetting

KEY = "options"  # the site_settings row
DEFAULTS: dict[str, Any] = {
    "homeCards": [{"kind": "latest"}, {"kind": "tag", "tag": "compilation"}, {"kind": "games"}],
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
    return {
        **{k: saved.get(k, v) for k, v in DEFAULTS.items()},
        "updatedAt": row.updated_at if row else None,
        "updatedBy": row.updated_by if row else None,
    }
