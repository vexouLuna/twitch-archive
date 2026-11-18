"""Custom uploads: the chunked store, the form's checks, the routes (dev DB + ffmpeg) and the remux step."""

import os
import shutil
import subprocess
import sys
import time

import pytest
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from archive_worker import custom_upload, planning
from archive_worker.context import StepRefused
from archive_worker.custom_upload import UploadError
from archive_worker.job_rows import RUNS
from archive_worker.steps import custom
from sqlalchemy import delete, text

from .test_api_v2 import KEY, VOD, admin, vod  # noqa: F401 - fixtures

NEEDS_FFMPEG = pytest.mark.skipif(
    not shutil.which("ffmpeg") or sys.platform == "win32",
    reason="ffmpeg, and asyncio subprocesses (not on Windows' selector loop)",
)


async def _body(*pieces: bytes):
    for p in pieces:
        yield p


async def test_chunks_go_in_order_and_resume_from_what_arrived(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_upload, "CHUNK_BYTES", 4)
    up = custom_upload.start(tmp_path, "  my  clip.mkv ", 10, 100, "someone")
    assert up["filename"] == "my clip.mkv" and up["received_bytes"] == 0 and custom_upload.ID.match(up["id"])
    uid = up["id"]
    assert (await custom_upload.write_chunk(tmp_path, uid, 0, _body(b"01", b"23")))["received_bytes"] == 4
    with pytest.raises(UploadError) as e:  # a resend of what arrived: told where to go on from
        await custom_upload.write_chunk(tmp_path, uid, 0, _body(b"0123"))
    assert e.value.code == "offset_mismatch" and e.value.extra == {"received_bytes": 4}
    with pytest.raises(UploadError) as e:  # too big: cut off again
        await custom_upload.write_chunk(tmp_path, uid, 4, _body(b"456", b"78"))
    assert e.value.code == "chunk_too_large"
    assert custom_upload.status(tmp_path, uid)["received_bytes"] == 4
    await custom_upload.write_chunk(tmp_path, uid, 4, _body(b"4567"))
    with pytest.raises(UploadError, match="at most 2 bytes"):  # past the declared size
        await custom_upload.write_chunk(tmp_path, uid, 8, _body(b"890"))
    await custom_upload.write_chunk(tmp_path, uid, 8, _body(b"89"))
    assert [u["received_bytes"] for u in custom_upload.listing(tmp_path)] == [10]
    with pytest.raises(UploadError) as e:  # complete, but not a video
        await custom_upload.take(tmp_path, uid, tmp_path / "out")
    assert e.value.code in ("not_video",)
    assert custom_upload.cancel(tmp_path, uid)["id"] == uid
    assert custom_upload.listing(tmp_path) == []
    with pytest.raises(UploadError) as e:
        custom_upload.status(tmp_path, uid)
    assert e.value.status == 404


def test_size_cap_and_ids(tmp_path):
    with pytest.raises(UploadError) as e:
        custom_upload.start(tmp_path, "big.mp4", 2 * 2**30, 2**30, None)
    assert e.value.status == 413
    with pytest.raises(UploadError):
        custom_upload.start(tmp_path, "empty.mp4", 0, 2**30, None)
    with pytest.raises(UploadError) as e:
        custom_upload.status(tmp_path, "../../etc")
    assert e.value.status == 404
    assert custom_upload.is_upload("upload-0123456789ab") and not custom_upload.is_upload("2703890458")


def test_abandoned_uploads_are_swept(tmp_path):
    old = custom_upload.start(tmp_path, "old.mp4", 5, 100, None)["id"]
    new = custom_upload.start(tmp_path, "new.mp4", 5, 100, None)["id"]
    then = time.time() - 30 * 3600
    for f in (custom_upload.upload_dir(tmp_path) / old).iterdir():
        os.utime(f, (then, then))
    assert custom_upload.sweep(tmp_path, 24) == [old]
    assert [u["id"] for u in custom_upload.listing(tmp_path)] == [new]
    assert custom_upload.sweep(tmp_path, 0) == []  # 0: off


def test_details_as_youtube_takes_them():
    assert custom_upload.check_details("  A   title ", " text \n", "unlisted") == ("A title", "text", "unlisted")
    for bad in [("", "", "private"), ("x" * 101, "", "private"), ("a <b>", "", "private"), ("t", "", "secret")]:
        with pytest.raises(UploadError):
            custom_upload.check_details(*bad)
    assert planning.custom_title("My clip", 1, 1) == "My clip"
    assert planning.custom_title("My clip", 2, 3) == "My clip (part 2/3)"
    assert len(planning.custom_title("x" * 120, 2, 3)) == 100


def _video(path, seconds=3):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=25:d={seconds}"]
    cmd += ["-f", "lavfi", "-i", f"sine=d={seconds}", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


@NEEDS_FFMPEG
async def test_upload_source_remuxes_the_file_once(make_ctx, tmp_path, monkeypatch):
    src = _video(tmp_path / "clip.mkv")
    ctx = make_ctx("custom_upload", "upload-0123456789ab", {"upload_file": str(src)})
    updates = {}

    async def update_vod(**values):
        updates.update(values)

    async def save():
        pass

    monkeypatch.setattr(ctx, "update_vod", update_vod)
    monkeypatch.setattr(ctx, "save", save)
    await custom.upload_source(ctx)
    assert ctx.default_mp4.exists() and not src.exists() and "upload_file" not in ctx.payload
    assert 2.5 < ctx.payload["duration"] < 3.5 and updates == {"duration": "00:00:03"}
    await custom.upload_source(ctx)  # again: kept
    gone = make_ctx("custom_upload", "upload-ffffffffffff", {"upload_file": str(tmp_path / "nope.mp4")})
    with pytest.raises(StepRefused, match="gone"):
        await custom.upload_source(gone)


async def _cleanup_uploads():
    """The custom VODs, their runs and their audit rows (as test_api_v2's _reset does for its VOD)."""
    async with get_sessionmaker()() as s:
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.like("upload:%")))
        await s.execute(delete(RUNS).where(RUNS.c.subject.like("vod:upload-%")))
        await s.execute(text("SET LOCAL search_path TO jobs, public"))
        await s.execute(text("DELETE FROM procrastinate_jobs WHERE lock LIKE 'vod:upload-%'"))
        await s.execute(delete(Vod).where(Vod.id.startswith("upload-")))
        await s.commit()


@NEEDS_FFMPEG
async def test_upload_finish_and_what_a_custom_vod_refuses(admin, settings, tmp_path, monkeypatch):  # noqa: F811
    from archive_worker import jobs

    monkeypatch.setattr(custom_upload, "CHUNK_BYTES", 64 * 1024)
    data = _video(tmp_path / "clip.mp4").read_bytes()
    await _cleanup_uploads()
    r = await admin.post("/api/v2/uploads", json={"filename": "clip.mp4", "size_bytes": len(data)}, headers=KEY)
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["chunk_bytes"] == 64 * 1024
    url = f"/api/v2/uploads/{up['id']}"
    sent = 0
    while sent < len(data):
        chunk = data[sent : sent + up["chunk_bytes"]]
        r = await admin.put(url, params={"offset": sent}, content=chunk, headers=KEY)
        assert r.status_code == 200, r.text
        sent = r.json()["received_bytes"]
    r = await admin.put(url, params={"offset": 0}, content=b"x", headers=KEY)
    assert r.status_code == 409 and r.json()["code"] == "offset_mismatch" and r.json()["received_bytes"] == len(data)
    r = await admin.post(f"{url}/finish", json={"title": "", "privacy": "private"}, headers=KEY)
    assert r.status_code == 422
    r = await admin.post(
        f"{url}/finish", json={"title": "My clip", "description": "hi", "privacy": "unlisted"}, headers=KEY
    )
    assert r.status_code == 201, r.text
    done = r.json()
    vid = done["vod_id"]
    assert vid == up["id"] and 2.5 < done["duration_seconds"] < 3.5
    try:
        async with get_sessionmaker()() as s:
            row = await s.get(Vod, vid)
        assert row.hidden and row.platform == "upload" and row.title == "My clip" and row.duration == "00:00:03"
        job = await jobs.get(done["job_id"])
        assert job.kind == "custom_upload" and job.payload["custom"]["privacy"] == "unlisted"
        assert (settings.vod_dir / vid / f"{vid}.upload").exists()
        assert (await admin.get(url, headers=KEY)).status_code == 404  # the upload is the VOD's now

        listed = (await admin.get("/api/v2/vods", params={"custom": "true"}, headers=KEY)).json()["items"]
        assert [v["id"] for v in listed] == [vid]
        assert vid not in [
            v["id"] for v in (await admin.get("/api/v2/vods", params={"custom": "false"}, headers=KEY)).json()["items"]
        ]
        r = await admin.patch(f"/api/v2/vods/{vid}", json={"hidden": False}, headers=KEY)
        assert r.status_code == 409 and r.json()["code"] == "vod_custom"
        r = await admin.post(f"/api/v2/vods/{vid}/rearchive", json={"type": "vod"}, headers=KEY)
        assert r.status_code == 409 and "custom upload" in r.json()["detail"]
        r = await admin.post(
            "/api/v2/synthetic", json={"id": "syn-of-upload", "segments": [{"vod_id": vid}], "title": "x"}, headers=KEY
        )
        assert r.status_code in (409, 422) and "custom upload" in r.text
    finally:
        await _cleanup_uploads()


@NEEDS_FFMPEG
async def test_a_file_that_is_not_a_video_is_refused_and_kept_to_cancel(admin, settings):  # noqa: F811
    data = b"not a video at all, just some text"
    up = (await admin.post("/api/v2/uploads", json={"filename": "x.mp4", "size_bytes": len(data)}, headers=KEY)).json()
    url = f"/api/v2/uploads/{up['id']}"
    assert (await admin.put(url, params={"offset": 0}, content=data, headers=KEY)).status_code == 200
    r = await admin.post(f"{url}/finish", json={"title": "t"}, headers=KEY)
    assert r.status_code == 422 and r.json()["code"] == "not_video"
    assert (await admin.delete(url, headers=KEY)).json()["id"] == up["id"]
    assert not (custom_upload.upload_dir(settings.data_dir) / up["id"]).exists()
