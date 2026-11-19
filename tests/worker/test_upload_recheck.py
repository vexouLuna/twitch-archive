"""A re-run's upload step asks YouTube whether the parts it uploaded before are still there (no DB needed)."""

from typing import Any

from archive_worker.steps.publish import _forget_deleted
from archive_worker.youtube import YouTubeLimitError


def _parts(n: int) -> list[dict[str, Any]]:
    return [{"number": i, "path": f"p{i}.mp4"} for i in range(1, n + 1)]


async def test_a_deleted_part_is_uploaded_again_and_the_rest_kept(make_ctx, deps, monkeypatch):
    asked: list[list[str]] = []

    async def videos(ids: list[str]) -> dict[str, dict[str, Any]]:
        asked.append(ids)
        return {"a": {"id": "a", "status": {"uploadStatus": "processed"}}, "c": {"status": {"uploadStatus": "deleted"}}}

    monkeypatch.setattr(deps.youtube, "videos", videos)
    uploaded = {"1": {"id": "a"}, "2": {"id": "b"}, "3": {"id": "c"}}
    assert await _forget_deleted(make_ctx("custom_upload"), _parts(3), uploaded) is True
    assert uploaded == {"1": {"id": "a"}} and asked == [["a", "b", "c"]]


async def test_ids_go_in_batches_of_50_and_nothing_uploaded_asks_nothing(make_ctx, deps, monkeypatch):
    asked: list[int] = []

    async def videos(ids: list[str]) -> dict[str, dict[str, Any]]:
        asked.append(len(ids))
        return {i: {"status": {"uploadStatus": "processed"}} for i in ids}

    monkeypatch.setattr(deps.youtube, "videos", videos)
    assert await _forget_deleted(make_ctx(), _parts(2), {}) is False and asked == []
    uploaded = {str(i): {"id": f"v{i}"} for i in range(1, 61)}
    assert await _forget_deleted(make_ctx(), _parts(60), uploaded) is False
    assert asked == [50, 10] and len(uploaded) == 60


async def test_when_youtube_cannot_say_every_part_is_kept(make_ctx, deps, monkeypatch):
    from types import SimpleNamespace

    from googleapiclient.errors import HttpError

    for exc in (
        YouTubeLimitError("quotaExceeded", "videos.list", HttpError(SimpleNamespace(status=403, reason="q"), b"{}")),
        HttpError(SimpleNamespace(status=500, reason="boom"), b"{}"),
        OSError("network down"),
    ):

        async def videos(ids: list[str], exc: Exception = exc) -> dict[str, dict[str, Any]]:
            raise exc

        monkeypatch.setattr(deps.youtube, "videos", videos)
        uploaded = {"1": {"id": "a"}}
        assert await _forget_deleted(make_ctx(), _parts(1), uploaded) is False
        assert uploaded == {"1": {"id": "a"}}
