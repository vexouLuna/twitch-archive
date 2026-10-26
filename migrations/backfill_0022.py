"""Tags ``vods`` on the real VODs that the old worker made between migration 0022 and the new worker
starting (ADR 0005, "Rollout"). Only that step of 0022: the rest isn't safe to run twice (its
``compilation`` → ``playthroughs`` step would catch the auto-updating VODs). Safe to re-run. From the
migrate service, as the superuser, like the migration itself:

    docker compose run --rm --entrypoint sh migrate -c 'P=$(cat /run/secrets/postgres_password);
      ARCHIVE_MIGRATION_DATABASE_URL="postgresql+asyncpg://postgres:$P@db:5432/archive" \
      exec python migrations/backfill_0022.py'
"""

import asyncio
import importlib.util
import os
from pathlib import Path

from archive_common.config import get_settings
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

spec = importlib.util.spec_from_file_location("m0022", Path(__file__).parent / "versions" / "0022_tag_model.py")
assert spec and spec.loader
m0022 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m0022)


def _run(conn: Connection) -> None:
    m0022._tag(conn, "vods", m0022.REAL_UNTAGGED)


async def main() -> None:
    engine = create_async_engine(os.environ.get("ARCHIVE_MIGRATION_DATABASE_URL") or get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
