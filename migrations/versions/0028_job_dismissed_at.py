"""vex-platform 0.10's ``dismissed_at`` on job runs: a dismissed failure stops counting as one (additive).

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-04
"""

from alembic import op
from vex_platform import migrations

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(5, schema="jobs"))


def downgrade() -> None:
    op.execute("DROP INDEX jobs.job_runs_undismissed")
    op.execute("ALTER TABLE jobs.job_runs DROP COLUMN dismissed_at")
