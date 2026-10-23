from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

# The first VOD of the first golden response: present only in a restored copy of the live database.
GOLDEN_VOD = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf-8"))[0]["body"]["data"][0]["id"]


@pytest.fixture(scope="module")
async def client():
    from archive_common.config import Settings, get_settings

    get_settings.cache_clear()
    settings = get_settings()
    settings.rate_limit_points = 1_000_000
    try:
        import sqlalchemy
        from archive_common.db import get_engine

        async with get_engine().connect() as conn:
            restored = await conn.scalar(sqlalchemy.text("select 1 from vods where id = :id"), {"id": GOLDEN_VOD})
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")
    if restored is None:  # pragma: no cover - environment dependent
        # An empty database (CI's): the golden responses were captured against the live data (docs/development.md).
        pytest.skip(f"the golden data isn't restored (no VOD {GOLDEN_VOD})")

    from archive_api.main import create_app

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    assert isinstance(settings, Settings)
