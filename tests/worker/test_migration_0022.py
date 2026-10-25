"""Migration 0022's VOD tags, up and down, in one transaction that's rolled back (needs the dev DB)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

PATH = Path(__file__).parents[2] / "migrations" / "versions" / "0022_tag_model.py"
spec = importlib.util.spec_from_file_location("m0022", PATH)
assert spec and spec.loader
m0022 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m0022)

ROWS = {  # id: (tags, synthetic)
    "test-0022-real": (["compilation"], None),
    "test-0022-made": (["compilation", "complete"], '{"by": "test"}'),
    "test-0022-auto": ([], '{"auto": {}}'),
}


def _tags(conn: sa.Connection) -> dict[str, list[str]]:
    rows = conn.execute(sa.text("SELECT id, tags FROM vods WHERE id LIKE 'test-0022-%'"))
    return {i: sorted(t) for i, t in rows}


def _check(conn: sa.Connection) -> None:
    for vod_id, (tags, synthetic) in ROWS.items():
        conn.execute(
            sa.text(
                "INSERT INTO vods (id, platform, title, created_at, tags, synthetic)"
                " VALUES (:id, 'twitch', 't', '2001-01-01T00:00:00Z', :tags, CAST(:syn AS jsonb))"
            ),
            {"id": vod_id, "tags": tags, "syn": synthetic},
        )
    with Operations.context(MigrationContext.configure(conn)):
        m0022.upgrade()
        assert _tags(conn) == {
            "test-0022-real": ["playthroughs", "vods"],
            "test-0022-made": ["complete", "playthroughs"],
            "test-0022-auto": ["compilation"],
        }
        m0022.downgrade()
        assert _tags(conn) == {
            "test-0022-real": ["compilation", "playthroughs", "vods"],
            "test-0022-made": ["compilation", "complete", "playthroughs"],
            "test-0022-auto": ["compilation"],
        }


async def test_compilation_only_stays_on_auto_and_comes_back_on_downgrade(db):
    async with db.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_check)
        finally:
            await trans.rollback()
