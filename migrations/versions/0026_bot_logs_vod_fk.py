"""bot_logs.vod_id gets a foreign key to vods, so deleting a VOD takes its bot chat with it.

0008 left the column bare, and ``DELETE /admin/delete`` left the rows behind. The orphans already there are
removed first, or the constraint would not apply. The downgrade drops the constraint, not the rows.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-02
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM bot_logs WHERE vod_id NOT IN (SELECT id FROM vods)")
    op.create_foreign_key(
        "bot_logs_vod_id_fkey", "bot_logs", "vods", ["vod_id"], ["id"], onupdate="CASCADE", ondelete="CASCADE"
    )


def downgrade() -> None:
    op.drop_constraint("bot_logs_vod_id_fkey", "bot_logs", type_="foreignkey")
