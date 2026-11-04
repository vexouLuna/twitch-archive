"""vex-platform 0.10's ``job_schedules`` table and ``schedule_id`` on job runs (additive).

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-04
"""

from alembic import op
from vex_platform import migrations

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migrations.apply(op, migrations.jobs_sql(6, schema="jobs"))


def downgrade() -> None:
    op.execute("DROP INDEX jobs.job_runs_schedule")
    op.execute("ALTER TABLE jobs.job_runs DROP COLUMN schedule_id")
    op.execute("DROP TABLE jobs.job_schedules")
