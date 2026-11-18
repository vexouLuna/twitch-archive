"""YouTube thumbnails: the template's checks, the drawing (ffmpeg), the thumbnail step and the routes (dev DB)."""

import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from archive_common.db import get_sessionmaker
from archive_common.models import AppState, Vod
from archive_worker import planning, thumbnails
from archive_worker.context import StepError, StepRefused
from archive_worker.steps import thumbnail as step
from archive_worker.thumbnails import TemplateError
from sqlalchemy import delete

from .test_api_v2 import KEY, VOD, admin, vod  # noqa: F401 - fixtures

NEEDS_FFMPEG = pytest.mark.skipif(
    not shutil.which("ffmpeg") or sys.platform == "win32",
    reason="ffmpeg, and asyncio subprocesses (not on Windows' selector loop)",
)
LINE = {"text": "PART {part}/{total}", "x": 0.1, "y": 0.1, "size": 0.1, "color": "#FFFFFF", "box": None}


def test_the_template_is_checked_and_tidied():
    assert thumbnails.check(thumbnails.DEFAULT) == thumbnails.DEFAULT
    got = thumbnails.check({"at": 1, "lines": [{**LINE, "text": "  PART   {part}/{total} "}]})
    assert got == {"at": 1.0, "lines": [{**LINE, "text": "PART {part}/{total}", "align": "left", "color": "#ffffff"}]}
    bad = [
        ([], "an object"),
        ({"at": 2, "lines": []}, "at must be"),
        ({"at": 0.5, "lines": [LINE] * 9}, "at most 8"),
        ({"at": 0.5, "lines": [{**LINE, "text": "{secret}"}]}, "unknown field {secret}"),
        ({"at": 0.5, "lines": [{**LINE, "text": "{part.__class__}"}]}, "braces are only for fields"),
        ({"at": 0.5, "lines": [{**LINE, "text": "a\nb"}]}, "one line"),
        ({"at": 0.5, "lines": [{**LINE, "color": "red"}]}, "color must be"),
        ({"at": 0.5, "lines": [{**LINE, "box": "#12345"}]}, "box must be"),
        ({"at": 0.5, "lines": [{**LINE, "align": "middle"}]}, "align must be"),
        ({"at": 0.5, "lines": [{**LINE, "size": 0.5}]}, "size must be"),
        ({"at": 0.5, "lines": [{**LINE, "font": "x"}]}, "unknown keys: font"),
        ({"at": 0.5, "lines": [], "extra": 1}, "unknown template keys"),
    ]
    for raw, msg in bad:
        with pytest.raises(TemplateError, match=msg.replace("{", r"\{").replace("}", r"\}").replace(".", r"\.")):
            thumbnails.check(raw)


def test_fields_games_and_label():
    assert thumbnails.fill("{label} PART {part}/{total} {nope}", {"label": "Live", "part": 2, "total": 3}) == (
        "Live PART 2/3"
    )
    assert thumbnails.fill("{games}", {"games": ""}) == ""  # an empty line, left out
    chapters = [
        {"name": "Doom", "start": 0, "end": 600},
        {"name": "Banned", "start": 600, "end": 600},
        {"name": "Celeste", "start": 1200, "end": 600},
        {"name": "Doom", "start": 1800, "end": 600},
    ]
    part = planning.Part(1, 300, 2000)
    assert planning.part_games(chapters, part, ["Banned"]) == ["Doom", "Celeste"]
    assert thumbnails.games_text(["Doom", "Celeste"]) == "Doom · Celeste"
    long = thumbnails.games_text([f"Game number {i}" for i in range(10)])
    assert len(long) <= thumbnails.GAMES_MAX and long.endswith("…")
    assert [thumbnails.label_of(k, t) for k, t in [("live", "live"), ("archive", "vod"), ("custom_upload", "vod")]] == [
        "Live",
        "VOD",
        "Upload",
    ]


def test_the_filter_graph_reads_text_from_files(tmp_path):
    t = thumbnails.check({"at": 0.5, "lines": [{**LINE, "text": "it's {title}:,;"}, {**LINE, "text": "{games}"}]})
    vf = thumbnails.filter_graph(t, {"title": "a'b"}, tmp_path)
    assert vf.startswith("scale=1280:720") and vf.count("drawtext=") == 1  # no games: that line is left out
    assert "expansion=none" in vf and "it's" not in vf
    assert (tmp_path / "line-0.txt").read_text(encoding="utf-8") == "it's a'b:,;"


def _video(path: Path, seconds: int = 4, size: str = "640x480") -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=25:d={seconds}"]
    subprocess.run([*cmd, "-c:v", "libx264", str(path)], check=True)
    return path


@NEEDS_FFMPEG
async def test_render_makes_a_1280x720_jpeg_within_youtubes_limit(tmp_path):
    values = {"part": 2, "total": 3, "games": "Doom · Celeste", "label": "VOD", "title": "x", "date": "", "channel": ""}
    for src in (_video(tmp_path / "v.mp4"), None):
        out = await thumbnails.render(src, 2.0, thumbnails.DEFAULT, values, tmp_path / "t.jpg")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,codec_name", "-of", "csv=p=0", str(out)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == "mjpeg,1280,720"
        assert 0 < out.stat().st_size <= thumbnails.MAX_BYTES
        assert not (tmp_path / "t-lines").exists()


# ── The step ──────────────────────────────────────────────────────────────


class FakeYouTube:
    def __init__(self, status: str = "allowed", fail: set[str] | None = None) -> None:
        self.status, self.fail = status, fail or set()
        self.set: list[str] = []

    async def own_channel(self):
        return {"id": "UC1", "title": "chan", "status": {"longUploadsStatus": self.status, "isLinked": True}}

    async def set_thumbnail(self, video_id, path):
        if video_id in self.fail:
            raise RuntimeError("YouTube said no")
        assert path.exists()
        self.set.append(video_id)


@pytest.fixture
def stepped(make_ctx, settings, monkeypatch, tmp_path):
    """A ctx factory for the thumbnail step: the VOD in memory, render writing a stub, no DB."""
    settings.youtube_thumbnail, settings.dry_run, settings.youtube_upload = True, False, True
    settings.live_record, settings.data_dir = False, tmp_path
    rendered = []

    async def render(src, at, template, values, out):
        rendered.append((src, round(at, 1), values["part"], values["games"], values["label"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return out

    async def load():
        return thumbnails.DEFAULT

    monkeypatch.setattr(thumbnails, "render", render)
    monkeypatch.setattr(thumbnails, "load", load)

    def make(kind, payload, youtube=None, yt=None):
        row = Vod(id="900", title="t", created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC), duration="00:20:00")
        row.youtube, row.platform = youtube or [], "twitch"
        row.chapters = [{"name": "Doom", "start": 0, "end": 1200}]
        ctx = make_ctx(kind, "900", payload)

        async def get_vod():
            return row

        async def save():
            pass

        monkeypatch.setattr(ctx, "get_vod", get_vod)
        monkeypatch.setattr(ctx, "save", save)
        monkeypatch.setattr(ctx.deps, "youtube", yt or FakeYouTube())
        return ctx

    make.rendered = rendered  # type: ignore[attr-defined]  # the renders, for the test to read
    return make


PARTS = [
    {"number": 1, "start": 0, "end": 600, "path": "/p/1.mp4"},
    {"number": 2, "start": 600, "end": 1200, "path": "/p/2.mp4"},
]
UPLOADED = {"1": {"id": "yt1"}, "2": {"id": "yt2"}}


async def test_the_step_sets_one_per_uploaded_part_and_never_fails_the_job(stepped):
    yt = FakeYouTube(fail={"yt1"})
    ctx = stepped("archive", {"parts": PARTS, "uploaded": UPLOADED, "total_parts": 2}, yt=yt)
    await step.thumbnail(ctx)
    assert yt.set == ["yt2"]
    assert ctx.payload["thumbnails"] == {"2": "yt2"} and "YouTube said no" in ctx.payload["thumbnail_failed"]["1"]
    one, two = (Path("/p/1.mp4"), 150.0, 1, "Doom", "VOD"), (Path("/p/2.mp4"), 150.0, 2, "Doom", "VOD")
    assert stepped.rendered == [one, two]
    yt.fail = set()
    await step.thumbnail(ctx)  # run again: only the one still missing
    assert yt.set == ["yt2", "yt1"]


async def test_an_unverified_channel_skips_with_a_warning_and_the_setting_turns_it_off(stepped, settings):
    yt = FakeYouTube(status="eligible")
    ctx = stepped("archive", {"parts": PARTS, "uploaded": UPLOADED}, yt=yt)
    await step.thumbnail(ctx)
    assert yt.set == [] and "verified channel" in ctx.payload["thumbnail_failed"]["all"]
    again = stepped("thumbnail", {"type": "vod"}, youtube=[{"id": "yt1", "type": "vod", "part": 1}], yt=yt)
    with pytest.raises(StepRefused, match="verified channel"):
        await step.thumbnail(again)
    settings.youtube_thumbnail = False
    off = stepped("archive", {"parts": PARTS, "uploaded": UPLOADED})
    await step.thumbnail(off)
    assert stepped.rendered == []


async def test_a_dry_run_only_draws_them(stepped, settings):
    settings.dry_run = True
    yt = FakeYouTube()
    await step.thumbnail(stepped("custom_upload", {"parts": PARTS}, yt=yt))
    assert yt.set == [] and [r[2] for r in stepped.rendered] == [1, 2]
    assert stepped.rendered[0][4] == "Upload"


async def test_again_downloads_a_few_seconds_of_each_upload(stepped, monkeypatch, tmp_path):
    fetched = []

    async def download(ctx, youtube_id, into, fmt, section):
        fetched.append((youtube_id, section))
        into.mkdir(parents=True, exist_ok=True)
        (into / "f.mp4").write_bytes(b"v")
        return into / "f.mp4"

    monkeypatch.setattr(step, "_download", download)
    yt = FakeYouTube(fail={"yt2"})
    youtube = [
        {"id": "yt1", "type": "vod", "part": 1, "duration": 600},
        {"id": "yt2", "type": "vod", "part": 2, "duration": 600},
        {"id": "lv1", "type": "live", "part": 1, "duration": 600},
    ]
    ctx = stepped("thumbnail", {"type": "vod", "only_parts": [1]}, youtube=youtube, yt=yt)
    await step.thumbnail(ctx)
    assert fetched == [("yt1", "*150-153")] and yt.set == ["yt1"]
    assert stepped.rendered[-1][1] == 0.5  # the frame: half a second into the download
    ctx = stepped("thumbnail", {"type": "vod", "only_parts": [2]}, youtube=youtube, yt=yt)
    with pytest.raises(StepError, match="no thumbnail made"):
        await step.thumbnail(ctx)


# ── Routes ────────────────────────────────────────────────────────────────


async def _forget_template():
    async with get_sessionmaker()() as s:
        await s.execute(delete(AppState).where(AppState.key == thumbnails.STATE_KEY))
        await s.commit()


async def test_template_routes(admin):  # noqa: F811
    await _forget_template()
    try:
        got = (await admin.get("/api/v2/thumbnail/template", headers=KEY)).json()
        assert got["saved"] is False and got["template"] == thumbnails.DEFAULT and "games" in got["fields"]
        r = await admin.put("/api/v2/thumbnail/template", json={"template": {"at": 3, "lines": []}}, headers=KEY)
        assert r.status_code == 422 and r.json()["code"] == "invalid_template"
        mine = {"at": 0.5, "lines": [{**LINE, "align": "center"}]}
        r = await admin.put("/api/v2/thumbnail/template", json={"template": mine}, headers=KEY)
        assert r.status_code == 200, r.text
        got = (await admin.get("/api/v2/thumbnail/template", headers=KEY)).json()
        assert got["saved"] is True and got["template"]["lines"][0]["color"] == "#ffffff"
    finally:
        await _forget_template()


async def test_thumbnails_again_needs_uploads_of_that_type(admin, vod):  # noqa: F811
    r = await admin.post(f"/api/v2/vods/{VOD}/thumbnails", json={"type": "compiled"}, headers=KEY)
    assert r.status_code == 409 and r.json()["code"] == "no_uploads"
    r = await admin.post("/api/v2/vods/nope/thumbnails", json={}, headers=KEY)
    assert r.status_code == 404


@NEEDS_FFMPEG
async def test_preview_draws_the_template(admin):  # noqa: F811
    r = await admin.post("/api/v2/thumbnail/preview", json={"template": thumbnails.DEFAULT}, headers=KEY)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    r = await admin.post("/api/v2/thumbnail/preview", json={"template": {"at": 0, "lines": [7]}}, headers=KEY)
    assert r.status_code == 422
