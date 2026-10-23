import shutil
import subprocess
import sys

import pytest
from archive_worker import ffmpeg, hls

pytestmark = [
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed"),
    # conftest.py runs the tests on a selector loop (the job runtime's psycopg needs one), which has
    # no subprocesses on Windows.
    pytest.mark.skipif(sys.platform == "win32", reason="asyncio subprocesses need the proactor loop on Windows"),
]


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """12 s test pattern with a tone, 1 s GOP so cuts land close to the target."""
    out = tmp_path_factory.mktemp("media") / "src.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "12",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],
        check=True,
    )
    return out


async def test_probe_and_cut(clip, tmp_path):
    assert abs(await ffmpeg.probe_duration(clip) - 12) < 0.2
    part = await ffmpeg.cut(clip, tmp_path / "p2.mp4", 5, 4)
    assert abs(await ffmpeg.probe_duration(part) - 4) < 1.1
    assert not (tmp_path / "p2.mp4.part").exists()
    tail = await ffmpeg.cut(clip, tmp_path / "tail.mp4", 7)  # no duration: to the end
    assert abs(await ffmpeg.probe_duration(tail) - 5) < 1.1


async def test_mute_and_blackout_keep_length(clip, tmp_path):
    muted = await ffmpeg.mute(clip, tmp_path / "m.mp4", [(1, 3), (6, 7)])
    assert abs(await ffmpeg.probe_duration(muted) - 12) < 0.3
    black = await ffmpeg.blackout(clip, tmp_path / "b.mp4", [(4, 6)], tmp_path / "work")
    assert abs(await ffmpeg.probe_duration(black) - 12) < 1.1
    many = await ffmpeg.blackout(clip, tmp_path / "b2.mp4", [(0, 1), (4, 6), (9, 12)], tmp_path / "work")
    assert abs(await ffmpeg.probe_duration(many) - 12) < 1.1
    assert not list((tmp_path / "work").glob("bo-*"))


async def test_hls_to_mp4(clip, tmp_path):
    seg_dir = tmp_path / "hls"
    seg_dir.mkdir()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(clip),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "4",
            "-segment_format",
            "mpegts",
            str(seg_dir / "%d.ts"),
        ],
        check=True,
    )
    names = sorted((p.name for p in seg_dir.glob("*.ts")), key=lambda n: int(n.split(".")[0]))
    (seg_dir / "index.m3u8").write_text(hls.write_local_playlist([(n, 4.0, False) for n in names]))
    out = await ffmpeg.hls_to_mp4(seg_dir / "index.m3u8", tmp_path / "out.mp4")
    assert abs(await ffmpeg.probe_duration(out) - 12) < 0.5


async def test_failure_leaves_no_output(tmp_path):
    with pytest.raises(ffmpeg.FfmpegError):
        await ffmpeg.cut(tmp_path / "missing.mp4", tmp_path / "x.mp4", 0, 1)
    assert not (tmp_path / "x.mp4").exists()


async def test_preview_sheets(clip, tmp_path):
    out = tmp_path / "previews" / "abcdefghijk"
    out.mkdir(parents=True)
    (out / "stale.jpg").write_bytes(b"old")
    assert await ffmpeg.preview_sheets(clip, out) == 2  # frames at 0 and 10 s of 12 s
    assert sorted(p.name for p in out.iterdir()) == ["0.jpg"]  # replaced whole
    assert not out.with_name(out.name + ".part").exists()
    size = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out / "0.jpg")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert size == "1600,900"  # 10×10 tiles of 160×90


async def test_encode_cuts_exactly(clip, tmp_path):
    a = await ffmpeg.encode(clip, tmp_path / "a.mp4", 1.3, 2.5)
    assert abs(await ffmpeg.probe_duration(a) - 2.5) < 0.1  # a stream copy would start at a keyframe


async def test_encode_converts_to_the_target(clip, tmp_path):
    assert await ffmpeg.probe_video(clip) == (160, 90, "10/1")
    a = await ffmpeg.encode(clip, tmp_path / "a.mp4", 1.0, 2.0, (320, 240, "25/1"))  # 16:9 into 4:3: padded
    assert await ffmpeg.probe_video(a) == (320, 240, "25/1")


async def test_concat(clip, tmp_path):
    a = await ffmpeg.cut(clip, tmp_path / "a.mp4", 0, 4, faststart=False)
    b = await ffmpeg.cut(clip, tmp_path / "b.mp4", 6, 4, faststart=False)
    joined = await ffmpeg.concat([a, b], tmp_path / "ab.mp4")
    assert abs(await ffmpeg.probe_duration(joined) - 8) < 2.2
    assert not (tmp_path / "ab.mp4.txt").exists()


async def test_concat_takes_a_path_with_a_quote(clip, tmp_path):
    folder = tmp_path / "it's"
    folder.mkdir()
    a = await ffmpeg.cut(clip, folder / "a.mp4", 0, 4, faststart=False)
    b = await ffmpeg.cut(clip, folder / "b.mp4", 6, 4, faststart=False)
    joined = await ffmpeg.concat([a, b], folder / "ab.mp4")
    assert abs(await ffmpeg.probe_duration(joined) - 8) < 2.2


def test_concat_listing_escapes_quotes_and_refuses_line_breaks(tmp_path):
    assert ffmpeg.concat_listing([tmp_path / "a'b.mp4"]) == f"file '{tmp_path.as_posix()}/a'\\''b.mp4'\n"
    with pytest.raises(ValueError):
        ffmpeg.concat_listing([tmp_path / "a\nfile 'x'"])
