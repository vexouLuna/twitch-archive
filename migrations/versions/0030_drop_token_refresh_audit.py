"""Delete the ``youtube.token.refresh`` rows from ``audit_log``: the worker no longer writes them (one per token
refresh, most of the trail) and nothing reads them; the stored token keeps ``refreshedAt``.

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-19
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    result = op.get_bind().execute(sa.text("DELETE FROM public.audit_log WHERE action = 'youtube.token.refresh'"))
    log.info("deleted %d youtube.token.refresh audit row(s)", result.rowcount)


def downgrade() -> None:
    # Nothing to do: the deleted rows are gone for good, and the schema did not change.
    pass
