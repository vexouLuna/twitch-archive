"""vex-platform 0.9.0's job events: the ``debug`` level, and an index for the newest progress report (additive).

vex-platform 0.9.0 logs ``debug`` lines and keeps only the newest progress report per run and step, apart
from the 20,000-line cap. The level check only widens, so an image still on 0.8.0 runs against it as before.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-26
"""

from alembic import op
from vex_platform import migrations

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(4, schema="jobs"))


def downgrade() -> None:
    # Keep the lines: the old check has no debug level.
    op.execute("DROP INDEX jobs.job_run_events_progress")
    op.execute("UPDATE jobs.job_run_events SET level = 'info' WHERE level = 'debug'")
    op.execute("ALTER TABLE jobs.job_run_events DROP CONSTRAINT job_run_events_level_check")
    op.execute(
        "ALTER TABLE jobs.job_run_events ADD CONSTRAINT job_run_events_level_check"
        " CHECK (level IN ('info', 'warning', 'error'))"
    )
