"""Why a VOD was cut (additive): ``vods.cut_reason``.

Free text set from the admin dashboard, shown on the site's page for a VOD whose every chapter was cut
from the YouTube uploads. NULL = no reason given (the site falls back to its own wording).

Revision ID: 0021
Revises: 0019
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
# shortcut: 0020 (tag types, twitch-archive#160) is still on its own branch; whichever of the two lands
# second points its down_revision at the other so the history keeps one head.
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vods", sa.Column("cut_reason", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("vods", "cut_reason")
