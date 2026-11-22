"""A re-run's upload step asks YouTube whether the parts it uploaded before are still on the connected channel, and
uploads again only the ones deleted from it (no DB needed)."""

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from archive_worker.context import JobContext
from archive_worker.steps import publish
from archive_worker.steps.publish import _forget_deleted
from archive_worker.youtube import YouTubeLimitError

CHANNEL = "UCours"
BEGAN = dt.datetime(2026, 11, 10, tzinfo=dt.UTC)


def _parts(n: int) -> list[dict[str, Any]]:
    return [{"number": i, "path": f"p{i}.mp4"} for i in range(1, n + 1)]


@pytest.fixture
def youtube(deps, monkeypatch):
    """YouTube with ``state.videos`` (id -> uploadStatus) visible, ``state.channel`` connected and the stored token
    ``state.token``; ``state.calls`` counts the API calls."""
    state = SimpleNamespace(videos={}, channel={"id": CHANNEL}, token={"connectedAt": "2026-11-01T00:00:00+00:00"})
    state.calls = []

    async def videos(ids: list[str]) -> dict[str, dict[str, Any]]:
        state.calls.append(("videos.list", len(ids)))
        return {i: {"id": i, "status": {"uploadStatus": state.videos[i]}} for i in ids if i in state.videos}

    async def own_channel() -> Any:
        state.calls.append(("channels.list", 1))
        return state.channel

    async def load_token() -> Any:
        return state.token

    monkeypatch.setattr(deps.youtube, "videos", videos)
    monkeypatch.setattr(deps.youtube, "own_channel", own_channel)
    monkeypatch.setattr(publish, "load_token", load_token)
    return state


@pytest.fixture
def run_ctx(deps):
    """A JobContext of a runtime run created at BEGAN."""
    run = SimpleNamespace(
        run=SimpleNamespace(created_at=BEGAN), payload={}, subject="vod:100", log=logging.getLogger("job")
    )
    return JobContext(7, "custom_upload", "100", {}, deps, run=run)


async def test_same_channel_deleted_is_uploaded_again(run_ctx, youtube):
    youtube.videos = {"a": "processed", "c": "deleted"}
    uploaded = {str(n): {"id": i, "channel": CHANNEL} for n, i in enumerate("abc", 1)}
    assert await _forget_deleted(run_ctx, _parts(3), uploaded) is True
    assert uploaded == {"1": {"id": "a", "channel": CHANNEL}}  # b missing, c deleted
    assert youtube.calls == [("videos.list", 3), ("channels.list", 1)]


async def test_another_channel_connected_skips_and_says_why(run_ctx, youtube, caplog):
    youtube.channel = {"id": "UCother"}
    uploaded = {"1": {"id": "a", "channel": CHANNEL}}
    with caplog.at_level(logging.WARNING):
        assert await _forget_deleted(run_ctx, _parts(1), uploaded) is False
    assert uploaded == {"1": {"id": "a", "channel": CHANNEL}}
    assert f"uploaded to channel {CHANNEL}, not UCother; skipping it" in caplog.text


async def test_a_channelless_account_skips(run_ctx, youtube):
    youtube.channel = None
    uploaded = {"1": {"id": "a", "channel": CHANNEL}}
    assert await _forget_deleted(run_ctx, _parts(1), uploaded) is False and "1" in uploaded


async def test_an_old_entry_counts_as_this_channel_when_connected_before_the_run(run_ctx, youtube):
    uploaded = {"1": {"id": "a"}}  # uploaded before entries recorded their channel
    assert await _forget_deleted(run_ctx, _parts(1), uploaded) is True and uploaded == {}


@pytest.mark.parametrize(
    "token",
    [{"connectedAt": "2026-11-15T00:00:00+00:00"}, {}],  # reconnected after the run began; imported legacy token
)
async def test_an_old_entry_is_skipped_when_the_account_may_have_changed(run_ctx, youtube, caplog, token):
    youtube.token = token
    uploaded = {"1": {"id": "a"}}
    with caplog.at_level(logging.WARNING):
        assert await _forget_deleted(run_ctx, _parts(1), uploaded) is False
    assert "1" in uploaded and "may be on another channel; skipping it" in caplog.text


async def test_an_old_entry_outside_a_runtime_run_is_skipped(make_ctx, youtube):
    uploaded = {"1": {"id": "a"}}
    assert await _forget_deleted(make_ctx(), _parts(1), uploaded) is False and "1" in uploaded


async def test_ids_go_in_batches_of_50_and_nothing_uploaded_asks_nothing(make_ctx, youtube):
    assert await _forget_deleted(make_ctx(), _parts(2), {}) is False and youtube.calls == []
    youtube.videos = {f"v{i}": "processed" for i in range(1, 61)}
    uploaded = {str(i): {"id": f"v{i}", "channel": CHANNEL} for i in range(1, 61)}
    assert await _forget_deleted(make_ctx(), _parts(60), uploaded) is False
    assert youtube.calls == [("videos.list", 50), ("videos.list", 10)] and len(uploaded) == 60  # no channels.list


async def test_when_youtube_cannot_say_every_part_is_kept(run_ctx, deps, youtube, monkeypatch):
    from googleapiclient.errors import HttpError

    fakes = {"videos": deps.youtube.videos, "own_channel": deps.youtube.own_channel}
    limit = YouTubeLimitError("quotaExceeded", "videos.list", HttpError(SimpleNamespace(status=403, reason="q"), b"{}"))
    for where, exc in (
        ("videos", limit),
        ("videos", HttpError(SimpleNamespace(status=500, reason="boom"), b"{}")),
        ("videos", OSError("network down")),
        ("own_channel", limit),  # the video is missing (deleted?), and the channel lookup fails
    ):

        async def fail(*_: Any, exc: Exception = exc) -> Any:
            raise exc

        for name, fake in fakes.items():
            monkeypatch.setattr(deps.youtube, name, fail if name == where else fake)
        uploaded = {"1": {"id": "a", "channel": CHANNEL}}
        assert await _forget_deleted(run_ctx, _parts(1), uploaded) is False
        assert uploaded == {"1": {"id": "a", "channel": CHANNEL}}
