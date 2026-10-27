"""Drop the ``games`` table, its NOTIFY triggers and its vod_id index.

v0.31.0 stopped reading and writing it (the per-game rows are gone from the API and the worker; chapters on
the VOD and ``synthetic.auto`` carry the game ids). The downgrade recreates the empty table, not its rows.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-27
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Dropping the table takes its triggers, index and grants with it.
    op.execute("DROP TABLE IF EXISTS games")


def downgrade() -> None:
    op.execute(
        """
CREATE TABLE IF NOT EXISTS games (
    id bigserial PRIMARY KEY,
    vod_id text NOT NULL REFERENCES vods(id) ON UPDATE CASCADE ON DELETE CASCADE,
    start_time numeric,
    end_time numeric,
    video_provider text,
    video_id text,
    thumbnail_url text,
    game_id text,
    game_name text,
    title text,
    chapter_image text,
    "createdAt" timestamptz NOT NULL,
    "updatedAt" timestamptz NOT NULL
)
"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS games_vod_id_idx ON games (vod_id)")
    op.execute(
        "CREATE TRIGGER games_notify_changed AFTER INSERT OR DELETE ON games "
        "FOR EACH ROW EXECUTE FUNCTION archive_notify_vod_changed()"
    )
    op.execute(
        "CREATE TRIGGER games_notify_updated AFTER UPDATE ON games "
        "FOR EACH ROW WHEN ((to_jsonb(OLD) - 'updatedAt') IS DISTINCT FROM (to_jsonb(NEW) - 'updatedAt')) "
        "EXECUTE FUNCTION archive_notify_vod_changed()"
    )
