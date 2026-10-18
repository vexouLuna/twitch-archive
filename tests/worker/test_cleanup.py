"""split with nothing to upload, and cleanup now or keep_files_days later."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from archive_worker.steps import media
from vex_platform.jobs import SkipTo


@pytest.mark.parametrize(("kind", "then"), [("archive", "cleanup"), ("live_file", None)])
async def test_split_with_nothing_to_upload_skips_to_cleanup(make_ctx, monkeypatch, kind, then):
    async def vod(ctx: Any = None, *_: Any) -> Any:
        return None

    async def duration(ctx: Any, vod: Any) -> float:
        return 600.0

    ctx = make_ctx(kind, "100", {"type": "vod"})
    monkeypatch.setattr(ctx, "get_vod", vod)
    monkeypatch.setattr(media, "vod_duration", duration)
    # The whole VOD is one restricted chapter.
    monkeypatch.setattr(media, "file_chapters", lambda ctx, vod: [{"name": "Artifact", "start": 0, "end": 600}])
    with pytest.raises(SkipTo, match="nothing to upload") as skip:
        await media.split(ctx)
    assert skip.value.step == then


def _files(ctx: Any) -> None:
    ctx.parts_dir.mkdir(parents=True)
    ctx.hls_dir.mkdir()
    ctx.default_mp4.write_bytes(b"mp4")


async def test_cleanup_now(make_ctx):
    ctx = make_ctx("archive", "100", {"type": "vod"})
    _files(ctx)
    await media.cleanup(ctx)
    assert not ctx.work_dir.exists()


async def test_cleanup_later_queues_a_cleanup_job(make_ctx, settings, monkeypatch):
    settings.keep_files_days = 2
    ctx = make_ctx("archive", "100", {"type": "live", "stream_id": "555", "parts": []})
    _files(ctx)
    queued: list[tuple[Any, ...]] = []

    async def enqueue(kind: str, vod_id: str | None, payload: dict[str, Any], **options: Any) -> int:
        queued.append((kind, vod_id, payload, options))
        return 7

    monkeypatch.setattr(ctx, "enqueue", enqueue)
    await media.cleanup(ctx)
    assert ctx.default_mp4.exists() and ctx.hls_dir.exists()  # kept for now
    [(kind, vod_id, payload, options)] = queued
    assert (kind, vod_id, payload) == ("cleanup", "100", {"type": "live", "stream_id": "555"})
    assert options["queued_key"] == str(ctx.work_dir)
    left = options["not_before"] - dt.datetime.now(dt.UTC)
    assert dt.timedelta(days=2) - dt.timedelta(minutes=1) < left <= dt.timedelta(days=2)

    # Then: the cleanup job itself removes them.
    later = make_ctx("cleanup", "100", payload)
    await media.cleanup(later)
    assert not later.work_dir.exists()
