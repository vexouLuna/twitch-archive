"""Migration 0027 gives stored gap chapters their kind, up and down, rolled back after (needs the dev DB)."""

import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

PATH = Path(__file__).parents[2] / "migrations" / "versions" / "0027_segment_gap_label.py"
spec = importlib.util.spec_from_file_location("m0027", PATH)
assert spec and spec.loader
m0027 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m0027)


def _ch(name, restricted, **extra):
    return {"name": name, "start": 0, "end": 5, "restricted": restricted, **extra}


OLD = [
    _ch("Just Chatting", False),
    _ch("Technical difficulties", True),
    _ch("Technical difficulties", True, kind="gap"),
]
NAMED_ONLY = [_ch("Technical difficulties", False)]  # not restricted: someone's own chapter, left alone


def _check(conn: sa.Connection) -> None:
    for vod_id, chapters in {"test-0027-old": OLD, "test-0027-own": NAMED_ONLY, "test-0027-none": []}.items():
        conn.execute(
            sa.text(
                'INSERT INTO vods (id, platform, title, "createdAt", "updatedAt", chapters)'
                " VALUES (:id, 'twitch', 't', now(), now(), CAST(:c AS jsonb))"
            ),
            {"id": vod_id, "c": json.dumps(chapters)},
        )
    with Operations.context(MigrationContext.configure(conn)):
        m0027.downgrade()
        m0027.upgrade()
        rows: dict[str, list[dict[str, object]]] = dict(
            conn.execute(sa.text("SELECT id, chapters FROM vods WHERE id LIKE 'test-0027-%'")).all()
        )
        assert [c.get("kind") for c in rows["test-0027-old"]] == [None, "gap", "gap"]
        assert rows["test-0027-own"] == NAMED_ONLY and rows["test-0027-none"] == []


async def test_gap_kind_backfill(db):
    async with db.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_check)
        finally:
            await trans.rollback()
