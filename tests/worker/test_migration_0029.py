"""Migration 0029 adds job_schedules and job_runs.schedule_id, up and down, rolled back after (needs the dev DB)."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from archive_worker.jobs import KINDS, SCHEDULABLE

PATH = Path(__file__).parents[2] / "migrations" / "versions" / "0029_job_schedules.py"
spec = importlib.util.spec_from_file_location("m0029", PATH)
assert spec and spec.loader
m0029 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m0029)

HAS = sa.text(
    "SELECT (SELECT count(*) FROM information_schema.tables"
    " WHERE table_schema = 'jobs' AND table_name = 'job_schedules'),"
    " (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'jobs' AND table_name = 'job_runs'"
    " AND column_name = 'schedule_id')"
)


def _check(conn: sa.Connection) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        m0029.downgrade()
        assert conn.execute(HAS).one() == (0, 0)
        m0029.upgrade()
        assert conn.execute(HAS).one() == (1, 1)


async def test_schedules_up_and_down(db):
    async with db.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.run_sync(_check)
        finally:
            await trans.rollback()


def test_schedulable_kinds_exist_and_need_no_vod():
    assert set(SCHEDULABLE) <= set(KINDS)
    assert "cleanup" not in SCHEDULABLE
