"""split with nothing to upload, and cleanup now or keep_files_days later."""

from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from archive_worker.context import JobContext
from archive_worker.steps import media
from vex_platform.actor import Actor
from vex_platform.jobs import JobRun, SkipTo


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


class FakeRuntime:
    def __init__(self, runs: list[Any]) -> None:
        self.runs = runs
        self.cancelled: list[int] = []

    async def list(self, *, states: Any = None, kind: Any = None, limit: int = 50) -> list[Any]:
        return [r for r in self.runs if (not states or r.state in states) and (kind is None or r.kind == kind)]

    async def cancel(self, run_id: int, **_: Any) -> None:
        self.cancelled.append(run_id)


def _run(id: int, state: str, kind: str = "archive", vod: str = "100", queued_key: str | None = None) -> JobRun:
    # The real type: a stand-in that accepts any attribute hid a read of a field JobRun doesn't have.
    now = dt.datetime.now(dt.UTC)
    return JobRun(
        id=id, kind=kind, subject=f"vod:{vod}", state=state, step=None, payload={"type": "vod"}, attempts=0,
        last_error=None, not_before=None, pause_before=None, pause_next=False, cancel_requested=False,
        queued_key=queued_key, active_key=None, actor=Actor("job", "1", "t", "job"), procrastinate_job_id=None,
        created_at=now, updated_at=now, started_at=None, finished_at=None,
    )  # fmt: skip


def _runtime_ctx(deps: Any, kind: str, me: Any, runs: list[Any]) -> Any:
    step = SimpleNamespace(
        run=me, runtime=FakeRuntime(runs), payload={"type": "vod"}, subject=me.subject, log=logging.getLogger("t")
    )
    return JobContext(me.id, kind, "100", step.payload, deps, run=step)


@pytest.mark.parametrize(
    ("other", "kept"),
    [
        (_run(9, "running"), True),  # a newer pipeline is working in the folder
        (_run(9, "failed"), True),  # failed after the cleanup was queued: a retry needs the files
        (_run(1, "failed"), False),  # failed before the run that queued the cleanup
        (_run(9, "running", vod="200"), False),  # another VOD's folder
        (_run(9, "running", kind="previews_fetch"), False),  # no cleanup step of its own to take over
    ],
)
async def test_delayed_cleanup_keeps_files_a_newer_run_uses(deps, other, kept):
    me = SimpleNamespace(id=5, parent_id=4, subject="vod:100")
    ctx = _runtime_ctx(deps, "cleanup", me, [other])
    _files(ctx)
    await media.cleanup(ctx)
    assert ctx.work_dir.exists() is kept


async def test_cleanup_later_cancels_an_earlier_waiting_cleanup(deps, settings, monkeypatch):
    settings.keep_files_days = 2
    me = SimpleNamespace(id=9, parent_id=None, subject="vod:100")
    ctx = _runtime_ctx(deps, "archive", me, [])
    ctx.run.runtime.runs = [
        _run(5, "queued", kind="cleanup", queued_key=str(ctx.work_dir)),
        _run(6, "queued", kind="cleanup", vod="200", queued_key="elsewhere"),
    ]

    async def enqueue(*_: Any, **__: Any) -> int:
        return 10

    monkeypatch.setattr(ctx, "enqueue", enqueue)
    await media.cleanup(ctx)
    assert ctx.run.runtime.cancelled == [5]
