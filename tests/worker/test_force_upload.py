"""force_upload_manual: the VOD copy of an archive started from the dashboard, with live_record on and
multi_track off (needs the dev DB)."""

import datetime as dt
from typing import Any

import pytest
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_worker import ffmpeg
from archive_worker.steps import publish
from sqlalchemy import delete

VOD = "test-force-upload-vod"


@pytest.fixture
async def vod(db):
    async def make(youtube: list[dict[str, Any]]) -> str:
        async with get_sessionmaker()() as s:
            await s.execute(delete(Vod).where(Vod.id == VOD))
            s.add(
                Vod(
                    id=VOD,
                    title="t",
                    created_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
                    duration="01:00:00",
                    youtube=youtube,
                )
            )
            await s.commit()
        return VOD

    yield make
    async with get_sessionmaker()() as s:
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


@pytest.fixture
def uploads(deps, settings, monkeypatch):
    settings.live_record, settings.multi_track, settings.youtube_public = True, False, True
    made: list[str] = []

    async def upload(path, *, privacy_status: str, **_: Any) -> dict[str, Any]:
        made.append(privacy_status)
        return {"id": f"new{len(made)}"}

    async def duration(path) -> float:
        return 3600.0

    monkeypatch.setattr(deps.youtube, "upload", upload)
    monkeypatch.setattr(ffmpeg, "probe_duration", duration)
    return made


def _payload(**extra: Any) -> dict[str, Any]:
    return {"type": "vod", "parts": [{"number": 1, "start": 0, "end": 3600, "path": "p1.mp4"}], **extra}


async def test_off_or_not_manual_uploads_nothing(vod, make_ctx, uploads, settings):
    await vod([])
    await publish.upload(make_ctx("archive", VOD, _payload(manual=True)))  # the setting is off
    settings.force_upload_manual = True
    await publish.upload(make_ctx("archive", VOD, _payload()))  # the monitor's own job
    assert uploads == []


async def test_a_manual_archive_without_a_live_copy_is_the_main_one(vod, make_ctx, uploads, settings):
    settings.force_upload_manual = True
    await vod([])
    ctx = make_ctx("archive", VOD, _payload(manual=True))
    await publish.upload(ctx)
    assert uploads == ["public"] and ctx.payload["uploaded"]["1"]["id"] == "new1"


async def test_next_to_a_live_copy_it_stays_unlisted(vod, make_ctx, uploads, settings):
    settings.force_upload_manual = True
    await vod([{"id": "live1", "type": "live", "duration": 3600, "part": 1}])
    await publish.upload(make_ctx("download", VOD, _payload(manual=True)))
    assert uploads == ["unlisted"]


def test_compiled_uploads_need_compile_youtube_upload(make_ctx, settings):
    from archive_worker.steps.publish import _publishing_disabled

    assert _publishing_disabled(make_ctx("compile", payload={"type": "compiled"})) == "compile_youtube_upload is off"
    settings.compile_youtube_upload = True
    assert _publishing_disabled(make_ctx("compile", payload={"type": "compiled"})) is None


async def test_a_403_on_upload_fails_at_once_with_the_channel_standing(vod, make_ctx, deps, settings, monkeypatch):
    from types import SimpleNamespace

    from archive_worker.context import StepRefused
    from googleapiclient.errors import HttpError

    settings.force_upload_manual = True
    await vod([])
    content = b'{"error": {"message": "Forbidden", "errors": [{"reason": "forbidden"}]}}'

    async def upload(path, **_: Any) -> dict[str, Any]:
        raise HttpError(SimpleNamespace(status=403, reason="Forbidden"), content)

    async def own_channel() -> dict[str, Any]:
        status = {"longUploadsStatus": "disallowed", "isLinked": True, "privacyStatus": "public"}
        return {"id": "UC1", "title": "chan", "url": "u", "status": status}

    async def cached_check() -> dict[str, Any]:
        return {"grantedScopes": ["https://www.googleapis.com/auth/youtube"]}

    monkeypatch.setattr(deps.youtube, "upload", upload)
    monkeypatch.setattr(deps.youtube, "own_channel", own_channel)
    monkeypatch.setattr(deps.youtube, "cached_check", cached_check)
    with pytest.raises(StepRefused, match=r"403 forbidden: Forbidden\). channel chan: long uploads disallowed"):
        await publish.upload(make_ctx("archive", VOD, _payload(manual=True)))
