"""Which jobs the monitor queues for a live stream (monitor.py; the DB calls are faked)."""

from typing import Any

import pytest
from archive_worker import jobs
from archive_worker import monitor as monitor_mod
from archive_worker.monitor import Monitor

STREAM, VOD = "990000000002", "2990000002"


@pytest.fixture
def run(settings, monkeypatch):
    rows: set[str] = set()  # stream ids that have a vods row
    queued: list[tuple[str, str | None]] = []

    class FakeHelix:
        def __init__(self) -> None:
            self.settings = settings

        async def get_stream(self, _user_id: str) -> dict[str, Any]:
            return {"id": STREAM, "started_at": "2026-10-08T12:00:00Z"}

        async def video_for_stream(self, _user_id: str, sid: str) -> dict[str, Any]:
            return {"id": VOD, "stream_id": sid, "title": "t", "created_at": "2026-10-08T12:00:00Z", "duration": ""}

    class FakeService:
        async def enqueue(self, kind: str, vod_id: str | None, _payload: dict[str, Any]) -> None:
            queued.append((kind, vod_id))

    async def exists_any(kind: str, stream_id: str) -> bool:
        return any(k == kind for k, _ in queued)

    async def upsert_vod(video: dict[str, Any]) -> None:
        rows.add(video["stream_id"])

    async def vod_id_for_stream(stream_id: str) -> str | None:
        return VOD if stream_id in rows else None

    async def nothing(*_args: Any, **_kw: Any) -> Any:
        return []

    monkeypatch.setattr(jobs, "exists_any", exists_any)
    monkeypatch.setattr(monitor_mod, "upsert_vod", upsert_vod)
    monkeypatch.setattr(monitor_mod, "vod_id_for_stream", vod_id_for_stream)
    monkeypatch.setattr(monitor_mod, "set_live_stream", nothing)
    monkeypatch.setattr(monitor_mod, "live_stream_ids", nothing)
    settings.vod_download, settings.doomtp_url = True, ""
    m = Monitor(FakeHelix(), FakeService(), settings)  # type: ignore[arg-type]

    async def ticks(live_record: bool, multi_track: bool) -> tuple[list[tuple[str, str | None]], set[str]]:
        settings.live_record, settings.multi_track = live_record, multi_track
        await m.tick()
        await m.tick()  # once per stream
        return queued, rows

    return ticks


@pytest.mark.parametrize(
    ("live_record", "multi_track", "expected"),
    [
        (False, False, [("archive", VOD)]),
        (False, True, [("archive", VOD)]),  # multi_track does nothing without live_record
        (True, True, [("live", None), ("archive", VOD)]),
        (True, False, [("live", None)]),  # the VOD copy is not uploaded: the live job saves the rest
    ],
)
async def test_jobs_per_stream(run, live_record, multi_track, expected):
    queued, rows = await run(live_record, multi_track)
    assert queued == expected
    assert rows == {STREAM}  # the vods row exists while the stream is live either way


@pytest.fixture
def ended(settings, monkeypatch):
    """A stream that was live and has now ended (get_stream -> None); the VOD is listed or not."""
    state: dict[str, Any] = {"video": None, "queued": [], "set_live": []}

    class FakeHelix:
        async def get_stream(self, _user_id: str) -> None:
            return None

        async def video_for_stream(self, _user_id: str, sid: str) -> dict[str, Any] | None:
            return state["video"]

    class FakeService:
        async def enqueue(self, kind: str, vod_id: str | None, _payload: dict[str, Any]) -> None:
            state["queued"].append((kind, vod_id))

    async def exists_any(kind: str, stream_id: str) -> bool:
        return False

    async def live_ids() -> list[str]:
        return [STREAM]

    async def set_live(stream_id: str | None, *_a: Any, keep: Any = ()) -> None:
        state["set_live"].append(set(keep))

    async def upsert_vod(_video: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(jobs, "exists_any", exists_any)
    monkeypatch.setattr(monitor_mod, "live_stream_ids", live_ids)
    monkeypatch.setattr(monitor_mod, "set_live_stream", set_live)
    monkeypatch.setattr(monitor_mod, "upsert_vod", upsert_vod)
    settings.doomtp_url = "http://bot"
    state["monitor"] = Monitor(FakeHelix(), FakeService(), settings)  # type: ignore[arg-type]
    return state


async def test_ended_without_vod_stays_live_then_retries(ended):
    m = ended["monitor"]
    await m.tick()
    assert ended["set_live"] == [{STREAM}]  # kept live: the next tick retries
    assert ended["queued"] == []
    ended["video"] = {"id": VOD, "stream_id": STREAM, "duration": ""}
    await m.tick()
    assert ended["queued"] == [("bot_chat", VOD)]
    assert ended["set_live"][-1] == set()  # done: now marked offline


async def test_ended_without_vod_gives_up(ended, monkeypatch):
    m = ended["monitor"]
    now = [1000.0]
    monkeypatch.setattr(monitor_mod.time, "monotonic", lambda: now[0])
    await m.tick()
    assert ended["set_live"][-1] == {STREAM}
    now[0] += monitor_mod.VOD_WAIT_SECONDS
    await m.tick()
    assert ended["set_live"][-1] == set()
    assert ended["queued"] == []
