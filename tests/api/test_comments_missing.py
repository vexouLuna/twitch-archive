"""A missing VOD's chat answers like a hidden one's (vexouLuna/twitch-archive#188), so the status doesn't say which."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from archive_api.comments import HIDDEN, Comments, _encode_cursor
from archive_api.errors import LegacyError
from archive_api.middleware import ResponseCache


@pytest.mark.parametrize("vod", [None, SimpleNamespace(hidden=True, merged_into=None, synthetic=None, createdAt=None)])
@pytest.mark.parametrize("by", ["offset", "cursor"])
async def test_missing_and_hidden_answer_the_same_404(monkeypatch, vod, by) -> None:
    comments = Comments(ResponseCache(300), ResponseCache(300))

    async def lookup(conn: Any, vod_id: str) -> Any:
        return vod

    async def counts(engine: Any, vod_id: str) -> dict[str, int]:
        return {"bot": 0, "replay": 0}

    async def nothing(*args: Any) -> None:
        return None

    async def unplayed(conn: Any, vod_id: str) -> bool:  # no shown synthetic VOD plays it (ADR 0005)
        return False

    monkeypatch.setattr(comments, "_vod", lookup)
    monkeypatch.setattr(comments, "_counts", counts)
    monkeypatch.setattr(comments, "_cursor_search", nothing)
    monkeypatch.setattr(comments, "_played", unplayed)
    engine = SimpleNamespace(connect=lambda: _Conn())
    offset, cursor = ("0", None) if by == "offset" else (None, _encode_cursor({"id": 1, "src": "replay"}))
    with pytest.raises(LegacyError) as err:
        await comments.handle(engine, "123", offset, cursor)  # type: ignore[arg-type]
    assert (err.value.status, err.value.msg) == (404, HIDDEN)


class _Conn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None
