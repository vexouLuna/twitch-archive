"""The censor song library: the routes (dev DB + ffmpeg), and a range whose song is gone failing its step."""

import json
import shutil
import subprocess
import sys

import pytest
from archive_worker import planning, songs
from archive_worker.context import StepRefused
from archive_worker.steps import media

from .test_api_v2 import KEY, VOD, admin, vod  # noqa: F401 - fixtures

NEEDS_FFMPEG = pytest.mark.skipif(
    not shutil.which("ffmpeg") or sys.platform == "win32",
    reason="ffmpeg, and asyncio subprocesses (not on Windows' selector loop)",
)


def test_song_ids():
    assert songs.is_id("song-0123456789ab")
    assert not songs.is_id("song-0123456789AB")
    assert not songs.is_id("song-../../etc/x")
    assert not songs.is_id("0123456789ab")


async def test_the_list_pages_newest_first(admin, settings):  # noqa: F811
    folder = songs.song_dir(settings.data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    made = [f"song-00000000000{i}" for i in range(3)]
    for i, sid in enumerate(made):
        meta = {"id": sid, "name": f"s{i}", "duration_seconds": 1.0, "size_bytes": 1, "created_by": None}
        meta["created_at"] = "2026-11-01T00:00:00+00:00" if i < 2 else "2026-11-02T00:00:00+00:00"
        (folder / f"{sid}.json").write_text(json.dumps(meta), encoding="utf-8")
        (folder / f"{sid}.m4a").write_bytes(b"")
    try:
        r = (await admin.get("/api/v2/songs", params={"limit": 2}, headers=KEY)).json()
        assert [s["id"] for s in r["items"]] == [made[2], made[1]] and r["next_cursor"]
        r = (await admin.get("/api/v2/songs", params={"limit": 2, "cursor": r["next_cursor"]}, headers=KEY)).json()
        assert [s["id"] for s in r["items"]] == [made[0]] and r["next_cursor"] is None
        r = await admin.get("/api/v2/songs", params={"cursor": "nonsense"}, headers=KEY)
        assert r.status_code == 400 and r.json()["code"] == "bad_cursor"
        assert (await admin.get("/api/v2/songs", params={"limit": 501}, headers=KEY)).status_code == 422
    finally:
        for sid in made:
            songs.delete(settings.data_dir, sid)


def test_a_range_whose_song_is_gone_fails_its_step(make_ctx, settings):
    plan = planning.plan_censor(
        [{"start": 60, "end": 70, "video": None, "mute": True, "audio": "song", "song": "song-0123456789ab"}]
    )
    with pytest.raises(StepRefused, match=r"00:01:00-00:01:10 play song\(s\) song-0123456789ab, no longer in the"):
        media._song_files(make_ctx(), plan)
    folder = songs.song_dir(settings.data_dir)
    folder.mkdir()
    (folder / "song-0123456789ab.m4a").write_bytes(b"x")
    assert media._song_files(make_ctx(), plan) == {"song-0123456789ab": folder / "song-0123456789ab.m4a"}


@pytest.fixture
def mp3(tmp_path):
    out = tmp_path / "tone.mp3"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:a", "libmp3lame", str(out)]
    subprocess.run(cmd, check=True)
    return out.read_bytes()


@NEEDS_FFMPEG
async def test_upload_list_play_use_and_delete_a_song(admin, mp3, monkeypatch):  # noqa: F811
    r = await admin.post("/api/v2/songs", params={"name": "  Elevator  music "}, content=mp3, headers=KEY)
    assert r.status_code == 201, r.text
    song = r.json()
    assert song["name"] == "Elevator music" and songs.is_id(song["id"])
    assert 1.9 < song["duration_seconds"] < 2.2 and song["size_bytes"] > 0

    assert [s["id"] for s in (await admin.get("/api/v2/songs", headers=KEY)).json()["items"]] == [song["id"]]
    r = await admin.get(f"/api/v2/songs/{song['id']}/audio", headers=KEY)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4" and r.content[4:8] == b"ftyp"

    ranges = [{"start": 1, "end": 2, "mute": True, "audio": "song", "song": song["id"], "volume": 0.5}]
    r = await admin.put(f"/api/v2/vods/{VOD}/censor", json={"censor": ranges}, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["censor"] == [{"video": None, **ranges[0]}]
    unknown = [dict(ranges[0], song="song-ffffffffffff")]
    r = await admin.put(f"/api/v2/vods/{VOD}/censor", json={"censor": unknown}, headers=KEY)
    assert r.status_code == 422 and "not in the song library" in r.json()["detail"]

    r = await admin.delete(f"/api/v2/songs/{song['id']}", headers=KEY)
    assert r.status_code == 409 and r.json()["code"] == "file_in_use"
    assert r.json()["file"] == {"kind": "song", "id": song["id"]} and r.json()["used_by"] == [
        {"kind": "vod", "id": VOD}
    ]
    assert (await admin.put(f"/api/v2/vods/{VOD}/censor", json={"censor": []}, headers=KEY)).status_code == 200
    r = await admin.delete(f"/api/v2/songs/{song['id']}", headers=KEY)
    assert r.status_code == 200 and r.json()["id"] == song["id"]
    assert (await admin.get("/api/v2/songs", headers=KEY)).json()["items"] == []
    assert (await admin.delete(f"/api/v2/songs/{song['id']}", headers=KEY)).status_code == 404


@NEEDS_FFMPEG
async def test_what_is_not_a_song_is_refused(admin, mp3, monkeypatch, settings):  # noqa: F811
    r = await admin.post("/api/v2/songs", params={"name": "x"}, content=b"<svg/> not audio at all", headers=KEY)
    assert r.status_code == 422 and r.json()["code"] == "not_audio"
    r = await admin.post("/api/v2/songs", params={"name": " "}, content=mp3, headers=KEY)
    assert r.status_code == 422 and r.json()["code"] == "invalid_song"
    monkeypatch.setattr(songs, "MAX_BYTES", 100)
    r = await admin.post("/api/v2/songs", params={"name": "big"}, content=mp3, headers=KEY)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert list(songs.song_dir(settings.data_dir).iterdir()) == []  # no upload left behind
