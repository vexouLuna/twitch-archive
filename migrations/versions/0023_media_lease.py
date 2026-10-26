"""One dashboard tab at a time streams a server file in the VOD preview (additive).

``media_leases`` holds who has it: one row per scope (``"server"`` for the whole server), the holding
tab's lease id, the actor, what to show the others (``holder``: login, browser, VOD, file) and when it
was taken and last renewed. A lease that missed its heartbeats is free to take.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_leases",
        sa.Column("scope", sa.Text, primary_key=True),
        sa.Column("lease_id", sa.Text, nullable=False),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("holder", JSONB, nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("media_leases")
