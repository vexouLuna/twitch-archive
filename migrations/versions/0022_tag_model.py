"""Tags say what a VOD is (data only; ADR 0005).

* The tag list, if saved: a tag is a home category exactly when it has a page (``homeCard`` follows it;
  one that had a card but no page gets its name as its page when free). ``playthroughs`` takes over
  ``compilation``'s look, page and card; ``compilation`` keeps neither. ``vods`` is added first, with
  the page ``vods``.
* The options, if saved: ``tag:compilation`` cards become ``tag:playthroughs``.
* VODs: every real one gets ``vods``; every ``compilation`` one ``playthroughs``; every ``auto`` one
  ``compilation``.
* Every merge or split (``synthetic.supersedes``): its sources are hidden (kept in ``synthetic.hid``, which
  deleting it shows again), it gets ``vods``, and ``supersedes`` goes.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-24
"""

import json
import re
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
RESERVED = {"vods", "live", "youtube", "games", "manage", "admin", "playthrough"}
EMPTY = {
    "drawn": False,
    "color": None,
    "width": None,
    "height": None,
    "rotate": None,
    **dict.fromkeys(("text", "textColor", "textSize", "textX", "textY", "textRotate")),
    **dict.fromkeys(("pattern", "patternColor", "patternSize")),
}
VODS = {"name": "vods", "label": "VOD", **EMPTY, "page": "vods", "pageLabel": "VODs", "homeCard": True}


def tags(saved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tag list as ADR 0005 has it."""
    out = [dict(t) for t in saved]
    pages = {t.get("page") for t in out if t.get("page")}
    for t in out:
        if t.get("homeCard") and not t.get("page") and t["name"] not in pages | RESERVED and NAME.fullmatch(t["name"]):
            t["page"], t["pageLabel"] = t["name"], t.get("label") or t["name"]
            pages.add(t["name"])
    names = [t["name"] for t in out]
    if "compilation" in names:
        i = names.index("compilation")
        comp = out[i]
        if "playthroughs" not in names:
            out.insert(i, {**comp, "name": "playthroughs"})
        else:  # its page, if it has none
            pt = out[names.index("playthroughs")]
            if not pt.get("page") and comp.get("page"):
                pt["page"], pt["pageLabel"] = comp["page"], comp.get("pageLabel")
        comp.update(label="auto-updating", page=None, pageLabel=None)
    if "vods" not in names:
        out.insert(0, VODS)
    for t in out:
        t["homeCard"] = bool(t.get("page"))
    return out


def cards(options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(options.get("homeCards"), list):
        return options
    renamed = [
        {**c, "tag": "playthroughs"} if isinstance(c, dict) and c.get("tag") == "compilation" else c
        for c in options["homeCards"]
    ]
    return {**options, "homeCards": [c for i, c in enumerate(renamed) if c not in renamed[:i]]}


def _setting(conn: sa.Connection, key: str, change: Any) -> None:
    value = conn.execute(sa.text("SELECT value FROM site_settings WHERE key = :k"), {"k": key}).scalar()
    if value:
        conn.execute(
            sa.text("UPDATE site_settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
            {"k": key, "v": json.dumps(change(value))},
        )


def _tag(conn: sa.Connection, tag: str, where: str) -> None:
    conn.execute(
        sa.text(f"UPDATE vods SET tags = array(SELECT unnest(tags || ARRAY[:t]) ORDER BY 1) WHERE {where}"),
        {"t": tag},
    )


def upgrade() -> None:
    conn = op.get_bind()
    _setting(conn, "tags", tags)
    _setting(conn, "options", cards)
    _tag(conn, "vods", "synthetic IS NULL AND NOT 'vods' = ANY(tags)")
    _tag(conn, "playthroughs", "'compilation' = ANY(tags) AND NOT 'playthroughs' = ANY(tags)")
    _tag(conn, "compilation", "synthetic ? 'auto' AND NOT 'compilation' = ANY(tags)")
    _tag(conn, "vods", "(synthetic->>'supersedes')::boolean IS TRUE AND NOT 'vods' = ANY(tags)")
    merges = conn.execute(
        sa.text("SELECT id FROM vods WHERE (synthetic->>'supersedes')::boolean IS TRUE ORDER BY id")
    ).scalars()
    for vod_id in list(merges):
        hid = conn.execute(
            sa.text(
                "UPDATE vods SET hidden = true WHERE NOT hidden AND id IN "
                "(SELECT source_id FROM vod_segments WHERE vod_id = :id) RETURNING id"
            ),
            {"id": vod_id},
        ).scalars()
        conn.execute(
            sa.text(
                "UPDATE vods SET synthetic = (synthetic - 'supersedes')"
                " || jsonb_build_object('hid', CAST(:hid AS jsonb)) WHERE id = :id"
            ),
            {"id": vod_id, "hid": json.dumps(sorted(hid))},
        )


def downgrade() -> None:
    pass  # data only; older code reads the new rows as ADR 0005's "Rollout" says
