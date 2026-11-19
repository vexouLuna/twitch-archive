"""ffmpeg / ffprobe subprocess wrappers.

Every command writes to ``<out>.part`` and renames on success, so a killed
worker never leaves a truncated file that looks finished.

The heavy operations (remux, cut, mute, cover/blackout, preview sheets) run one of a
type at a time across the worker's jobs (``_one_at_a_time``); a job that wants one
while another job has it waits its turn. ``probe_duration`` is cheap and never waits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import weakref
from collections.abc import Callable, Coroutine, Sequence
from functools import wraps
from pathlib import Path
from typing import Any

from archive_common import previews as pv

from . import font
from .planning import Cover, Covers, Sound

log = logging.getLogger(__name__)

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
# The font censor text is drawn in: the worker image's DejaVu Sans Bold (fonts-dejavu-core).
CENSOR_FONT = os.environ.get("CENSOR_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


class FfmpegError(RuntimeError):
    pass


# One lock per (event loop, operation): an asyncio.Lock belongs to the loop it first waits on.
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()


type _Op[**P, T] = Callable[P, Coroutine[Any, Any, T]]


def _one_at_a_time[**P, T](op: str) -> Callable[[_Op[P, T]], _Op[P, T]]:
    """Run the decorated operation under the ``op`` lock: one ``op`` at a time in this worker, while
    different ops still overlap. Waiting can be cancelled like the run itself."""

    def wrap(fn: _Op[P, T]) -> _Op[P, T]:
        @wraps(fn)
        async def run(*args: P.args, **kwargs: P.kwargs) -> T:
            lock = _locks.setdefault(asyncio.get_running_loop(), {}).setdefault(op, asyncio.Lock())
            if lock.locked():
                log.info("ffmpeg %s: waiting for the one already running", op)
            async with lock:
                return await fn(*args, **kwargs)

        return run

    return wrap


async def _run(args: list[str]) -> str:
    log.debug("exec: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace")[-2000:]
        raise FfmpegError(f"{Path(args[0]).name} exited with {proc.returncode}: {tail}")
    return out.decode("utf-8", "replace")


async def _ffmpeg_to(out: Path, args: list[str]) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    await _run([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args, "-f", "mp4", str(tmp)])
    os.replace(tmp, out)
    return out


async def probe_duration(path: Path) -> float:
    out = await _run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    return float(json.loads(out)["format"]["duration"])


async def run_probe(args: list[str]) -> str:
    """ffprobe with ``args`` (errors only), its stdout."""
    return await _run([FFPROBE, "-v", "error", *args])


@_one_at_a_time("audio")
async def encode_audio(src: Path, out: Path, max_seconds: float) -> Path:
    """The first audio stream of ``src`` as stereo 48 kHz AAC in an MP4 (``.m4a``), at most ``max_seconds``
    long, without its tags or cover art."""
    return await _ffmpeg_to(
        out,
        ["-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-t", f"{max_seconds:g}"]
        + ["-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    )


async def probe_video(path: Path) -> tuple[int, int, str]:
    """The first video stream's width, height and frame rate (ffprobe's fraction, e.g. ``30000/1001``)."""
    fields = "stream=width,height,avg_frame_rate,r_frame_rate"
    args = ["-v", "error", "-select_streams", "v:0", "-show_entries", fields, "-of", "json", str(path)]
    out = await _run([FFPROBE, *args])
    s = json.loads(out)["streams"][0]
    rate = s.get("avg_frame_rate", "0/0")
    return int(s["width"]), int(s["height"]), s["r_frame_rate"] if rate.startswith("0/") else rate


@_one_at_a_time("remux")
async def hls_to_mp4(playlist: Path, out: Path, *, fmp4: bool = False) -> Path:
    extra = ["-avoid_negative_ts", "make_zero", "-fflags", "+genpts"] if fmp4 else []
    return await _ffmpeg_to(
        out,
        [
            "-thread_queue_size",
            "1024",
            "-allowed_extensions",
            "ALL",
            "-i",
            str(playlist),
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            "-movflags",
            "+faststart",
            *extra,
        ],
    )


@_one_at_a_time("remux")
async def remux(src: Path, out: Path) -> Path:
    """The first video stream and the first audio stream (if any) of ``src`` into an MP4, stream-copied,
    index in front (a custom upload's file, whatever container it came in)."""
    return await _ffmpeg_to(
        out,
        ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn", "-movflags", "+faststart"],
    )


# Input seeking (``-ss`` before ``-i``) jumps straight to a keyframe; output seeking
# (``-ss`` after ``-i``) then trims exactly, but reads every packet before its target.
# Seeking in two stages gets both: the jump lands SEEK_MARGIN seconds early, well
# over Twitch's 2 s keyframe interval, and the exact trim only reads that margin.
# With stream copy the result is packet-for-packet identical to output seeking alone.
SEEK_MARGIN = 30.0


def _seek_args(src: Path, start: float) -> list[str]:
    """Input arguments that start reading ``src`` at ``start`` seconds."""
    if start <= 0:
        return ["-i", str(src)]
    margin = min(SEEK_MARGIN, start)
    return ["-ss", f"{start - margin:.3f}", "-i", str(src), "-ss", f"{margin:.3f}"]


@_one_at_a_time("cut")
async def cut(src: Path, out: Path, start: float, duration: float | None = None, *, faststart: bool = True) -> Path:
    """Stream-copy ``duration`` seconds (default: to the end) starting at ``start``.

    ``faststart=False`` skips the second pass that moves the index to the front,
    for intermediate files that are only joined again.
    """
    length = ["-t", f"{duration:.3f}"] if duration is not None else []  # a length, not an end
    return await _ffmpeg_to(
        out,
        [
            *_seek_args(src, start),
            *length,
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            *(["-movflags", "+faststart"] if faststart else []),
        ],
    )


@_one_at_a_time("previews")
async def preview_sheets(src: Path, out_dir: Path) -> int:
    """Seek-bar sprite sheets of ``src`` into ``out_dir`` (``archive_common.previews`` has the layout); the
    number of frames. Only keyframes are decoded (Twitch has one every 2 s), so a 1080p part takes a minute or
    two. ``round=up`` takes, for each tile, the last frame at or before its time (``near`` would take one up to
    half an interval late). The sheets go to ``<out_dir>.part`` first and replace ``out_dir`` whole."""
    frames = pv.frame_count(await probe_duration(src))
    tmp = out_dir.with_name(out_dir.name + ".part")
    await asyncio.to_thread(shutil.rmtree, tmp, True)
    tmp.mkdir(parents=True)
    w, h = pv.WIDTH, pv.HEIGHT
    vf = (
        f"fps=1/{pv.INTERVAL}:round=up,scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,tile={pv.COLS}x{pv.ROWS}"
    )
    try:
        await _run(
            [
                FFMPEG,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-skip_frame",
                "nokey",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-vf",
                vf,
                "-frames:v",
                str(pv.sheet_count(frames)),
                "-q:v",
                "5",
                "-start_number",
                "0",
                str(tmp / "%d.jpg"),
            ]
        )
        await asyncio.to_thread(shutil.rmtree, out_dir, True)
        os.replace(tmp, out_dir)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)
    return frames


@_one_at_a_time("mute")
async def mute(src: Path, out: Path, ranges: list[tuple[float, float]]) -> Path:
    """Silence audio in [start, end) ranges; video is stream-copied."""
    filters = ",".join(f"volume=0:enable='between(t,{a:g},{b:g})'" for a, b in ranges)
    return await _ffmpeg_to(
        out,
        [
            "-i",
            str(src),
            "-map",
            "0",
            "-c:v",
            "copy",
            "-af",
            filters,
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
        ],
    )


# Muffling: the audio as through a wall, a steep low-pass (two passes) a little louder to make up for it.
MUFFLE = "lowpass=f=400:enable='{on}',lowpass=f=400:enable='{on}',volume=1.5:enable='{on}'"


def sound_args(
    spans: list[tuple[float, float, Sound]], songs: dict[str, Path], from_input: int = 1
) -> tuple[list[str], str]:
    """The extra inputs and the ``-filter_complex`` graph (``[0:a]`` in, ``[aout]`` out) for ``spans`` as
    ``planning.plan_censor`` returns them: in list order, each over what the ones before it made. Silence and
    muffling are filters enabled over their span; a song silences its span, then its file, looped
    (``-stream_loop -1``) for the span's length, at its volume and delayed to the span's start, is mixed in,
    so a later range treats the song too."""
    inputs: list[str] = []
    steps: list[str] = []
    chain: list[str] = []  # filters on ``cur`` not yet written out
    cur, n = "0:a", from_input
    for i, (a, b, sound) in enumerate(spans):
        on = f"between(t,{a:g},{b:g})"
        if sound.audio == "muffle":
            chain.append(MUFFLE.format(on=on))
            continue
        chain.append(f"volume=0:enable='{on}'")
        if sound.audio == "song":
            inputs += ["-stream_loop", "-1", "-t", f"{b - a:.3f}", "-i", str(songs[str(sound.song)])]
            steps.append(f"[{cur}]{','.join(chain)}[m{i}]")
            steps.append(f"[{n}:a]volume={sound.volume:g},adelay=delays={round(a * 1000)}:all=1[s{i}]")
            steps.append(f"[m{i}][s{i}]amix=inputs=2:duration=first:normalize=0[x{i}]")
            cur, chain, n = f"x{i}", [], n + 1
    steps.append(f"[{cur}]{','.join(chain) or 'anull'}[aout]")
    return inputs, ";".join(steps)


@_one_at_a_time("mute")
async def censor_audio(src: Path, out: Path, spans: list[tuple[float, float, Sound]], songs: dict[str, Path]) -> Path:
    """Silence, muffle or put a song over the audio in ``spans`` (``sound_args``); video is stream-copied.
    ``songs``: each song id the spans play -> its file."""
    inputs, graph = sound_args(spans, songs)
    return await _ffmpeg_to(
        out,
        [
            "-i",
            str(src),
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "0:v?",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
        ],
    )


# Video filters for a range covering the whole frame: black (as DMCA blackouts always were), or a box blur a
# twentieth of the frame wide, run three times, too strong to make anything out (the comma is escaped for the
# filter graph). The other covers, and every box, are built by ``cover_filter``.
COVER_FILTERS = {"black": "geq=0:128:128", "blur": r"boxblur=luma_radius=min(w\,h)/20:luma_power=3"}
# x264 quality for a covered clip: a full-frame cover hides everything anyway; a box leaves the rest to see.
CRF_FULL, CRF_BOX = 30, 20


def _even(v: float) -> int:
    return math.floor(v / 2 + 0.5) * 2  # half up: round() goes to even, 4.5 -> 4


def _box(region: tuple[float, float, float, float] | None, width: int, height: int) -> tuple[int, int, int, int]:
    """A region (fractions) in pixels, on even numbers (yuv420's chroma is half size), at least 4 px, inside."""
    x, y, w, h = region or (0.0, 0.0, 1.0, 1.0)
    pw = min(max(_even(w * width), 4), width - width % 2)
    ph = min(max(_even(h * height), 4), height - height % 2)
    px = min(_even(x * width), width - pw)
    py = min(_even(y * height), height - ph)
    return max(px, 0), max(py, 0), pw, ph


def _quoted(path: Path) -> str:
    """A file path as a filter option value: quoted for the option, the quotes escaped for the graph."""
    text = path.as_posix()
    if any(c in text for c in "'\\\n\r"):
        raise ValueError(f"a filter can't name a path with a quote, backslash or line break: {text!r}")
    return f"\\'{text}\\'"


def text_size(box: tuple[int, int, int, int], text: str) -> int:
    """The font size (px) for one line of ``text`` in ``box`` (x, y, w, h px): half the box's height at most, and
    narrow enough that the line, measured in ``CENSOR_FONT`` (``font.advance``) with its outline (a twelfth of the size
    each side), takes at most 90% of the box's width; 10 at least. Integer maths, so vods-core's preview agrees."""
    width, units = font.advance(CENSOR_FONT, text)
    fits = 54 * box[2] * units // (10 * (6 * width + units))  # 0.9 * w / (width / units + 1 / 6)
    return max(10, min(box[3] // 2, fits))


def text_filter(text_file: Path, box: tuple[int, int, int, int], text: str) -> str:
    """``drawtext`` of ``text_file`` (holding the one line ``text``) centred in ``box`` (x, y, w, h px): white with a
    black outline, as large as fits (``text_size``). The text is read from the file with expansion off, so nothing
    in it is ffmpeg syntax."""
    x, y, w, h = box
    size = text_size(box, text)
    return (
        f"drawtext=fontfile={_quoted(Path(CENSOR_FONT))}:textfile={_quoted(text_file)}:expansion=none"
        f":fontsize={size}:fontcolor=white:borderw={max(1, size // 12)}:bordercolor=black"
        f":x={x}+({w}-text_w)/2:y={y}+({h}-text_h)/2"
    )


def cover_filter(covers: Covers, width: int, height: int, text_files: Sequence[Path | None] = ()) -> str:
    """The ``-vf`` graph for one covered clip of a ``width`` x ``height`` video: each cover drawn in turn.
    A fill is ``drawbox``; a blur or pixelation is the box cropped out, changed and laid back over the frame
    (the blur's radius and the pixel blocks scale with the box, so a small box is as unreadable as a big one).
    A cover with ``text`` then gets it written over its box from ``text_files[i]`` (``text_filter``)."""
    texts = [text_files[i] if c.text and i < len(text_files) else None for i, c in enumerate(covers)]
    if len(covers) == 1 and covers[0].region is None and covers[0].video in COVER_FILTERS and not any(texts):
        return COVER_FILTERS[covers[0].video]
    steps: list[str] = []
    cur = "0"
    for i, c in enumerate(covers):
        x, y, w, h = _box(c.region, width, height)
        out = f"c{i + 1}"
        if c.video in ("black", "color"):
            color = "black" if c.video == "black" else f"0x{(c.color or '#000000')[1:]}"
            steps.append(f"[{cur}]drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill[{out}]")
        elif c.video == "blur":
            radius = max(1, min(w, h) // 8)  # at most a quarter of the chroma plane's box
            steps.append(
                f"[{cur}]split[b{i}][t{i}];[t{i}]crop={w}:{h}:{x}:{y},boxblur=luma_radius={radius}:luma_power=3[f{i}];"
                f"[b{i}][f{i}]overlay={x}:{y}[{out}]"
            )
        elif c.video == "pixelate":
            block = max(4, min(w, h) // 8)  # about eight blocks across the box's short side
            sw, sh = max(1, -(-w // block)), max(1, -(-h // block))
            steps.append(
                f"[{cur}]split[b{i}][t{i}];[t{i}]crop={w}:{h}:{x}:{y},scale={sw}:{sh}:flags=neighbor,"
                f"scale={w}:{h}:flags=neighbor[f{i}];[b{i}][f{i}]overlay={x}:{y}[{out}]"
            )
        else:
            raise ValueError(f"unknown cover {c.video!r}")
        text_file = texts[i]
        if c.text and text_file is not None:
            steps.append(f"[{out}]{text_filter(text_file, (x, y, w, h), c.text)}[{out}t]")
            out = f"{out}t"
        cur = out
    # A simple (-vf) graph: its input and output pads are the unlabelled ends, so drop the first and last label.
    graph = ";".join(steps)
    return graph.removeprefix("[0]").removesuffix(f"[{cur}]")


def _text_file(path: Path, text: str | None) -> Path | None:
    if not text:
        return None
    path.write_text(text, encoding="utf-8")
    return path


async def blackout(src: Path, out: Path, ranges: list[tuple[float, float]], work: Path) -> Path:
    """Replace video in each [start, end) range with black, keeping audio, in one pass (see ``cover``).
    ``ranges`` must be sorted and non-overlapping, as ``planning.plan_dmca`` returns them."""
    return await cover(src, out, [(a, b, (Cover("black"),)) for a, b in ranges], work)


@_one_at_a_time("cover")
async def cover(src: Path, out: Path, ranges: list[tuple[float, float, Covers]], work: Path) -> Path:
    """Cover the video in each [start, end) range as ``cover_filter`` draws it, keeping audio, in one pass.

    Only those clips are re-encoded; the stretches around them are stream-copied, and all pieces are
    joined once with the concat demuxer (as the legacy code did per range). ``ranges`` must be sorted
    and non-overlapping, as ``planning.plan_dmca`` and ``planning.plan_censor`` return them.
    """
    work.mkdir(parents=True, exist_ok=True)
    total = await probe_duration(src)
    simple = all(
        len(c) == 1 and c[0].region is None and c[0].video in COVER_FILTERS and not c[0].text for _, _, c in ranges
    )
    width, height = (0, 0) if simple else (await probe_video(src))[:2]
    pieces: list[Path] = []
    scratch: list[Path] = []  # the text files, removed with the pieces
    pos = 0.0
    try:
        for i, (start, end, covers) in enumerate(ranges):
            if start > pos:
                pieces.append(await cut(src, work / f"bo-{i}-copy.mp4", pos, start - pos, faststart=False))
            boxed = any(c.region is not None for c in covers)
            text_files = [_text_file(work / f"bo-{i}-text-{j}.txt", c.text) for j, c in enumerate(covers)]
            scratch += [p for p in text_files if p]
            pieces.append(
                await _ffmpeg_to(
                    work / f"bo-{i}-black.mp4",
                    [
                        "-ss",
                        f"{start:.3f}",
                        "-i",
                        str(src),
                        "-t",
                        f"{end - start:.3f}",
                        "-map",
                        "0",
                        "-vf",
                        cover_filter(covers, width, height, text_files),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        str(CRF_BOX if boxed else CRF_FULL),
                        "-c:a",
                        "copy",
                    ],
                )
            )
            pos = end
        if pos < total - 0.05:
            pieces.append(await cut(src, work / "bo-tail.mp4", pos, faststart=False))
        listing = work / "bo-list.txt"
        listing.write_text(concat_listing(pieces), encoding="utf-8")
        pieces.append(listing)
        return await _ffmpeg_to(
            out, ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart"]
        )
    finally:
        for p in pieces + scratch:
            p.unlink(missing_ok=True)


@_one_at_a_time("encode")
async def encode(
    src: Path, out: Path, start: float, duration: float, video: tuple[int, int, str] | None = None
) -> Path:
    """Re-encode ``duration`` seconds from ``start``, cut on the exact frame (stream copy cuts at keyframes).
    H.264 + AAC, the first video and audio stream only, so clips of the same settings concat as copies.
    ``video`` (width, height, frame rate) scales to fit inside that size, pads the rest black (the aspect
    ratio is kept) and converts the frame rate, so windows of different sources join cleanly."""
    vf = []
    if video:
        w, h, rate = video
        vf = [
            "-vf",
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate}",
        ]
    return await _ffmpeg_to(
        out,
        [
            "-ss",
            f"{start:.3f}",
            "-i",
            str(src),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            *vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
        ],
    )


@_one_at_a_time("still")
async def still(src: Path | None, at: float, out: Path, vf: str, quality: int = 3) -> Path:
    """One frame of ``src`` at ``at`` seconds through the filter graph ``vf``, as the JPEG ``out`` (``quality``:
    ffmpeg's -q:v, 2 best to 31). No ``src``: a plain dark 1280x720 frame, for a template's preview."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.unlink(missing_ok=True)
    source = (
        ["-f", "lavfi", "-i", "color=c=0x1b2130:s=1280x720:d=1"]
        if src is None
        else ["-ss", f"{max(0.0, at):.3f}", "-i", str(src)]
    )
    await _run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *source, "-frames:v", "1", "-vf", vf]
        + ["-q:v", str(quality), "-c:v", "mjpeg", "-f", "image2", str(tmp)]
    )
    os.replace(tmp, out)
    return out


def concat_listing(paths: list[Path]) -> str:
    """A concat-demuxer list of ``paths``: each quoted, a ``'`` written as ``'\\''`` (ffmpeg's escape),
    so a path can't end its own line early and add directives. A newline can't be quoted at all."""
    lines = []
    for p in paths:
        text = p.as_posix()
        if "\n" in text or "\r" in text:
            raise ValueError(f"a concat list can't hold a path with a line break: {text!r}")
        quoted = text.replace("'", "'\\''")
        lines.append(f"file '{quoted}'\n")
    return "".join(lines)


@_one_at_a_time("concat")
async def concat(parts: list[Path], out: Path) -> Path:
    """Join stream-copied clips of the same encoding end to end (the concat demuxer), index in front."""
    listing = out.with_name(out.name + ".txt")
    listing.write_text(concat_listing(parts), encoding="utf-8")
    try:
        return await _ffmpeg_to(
            out,
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0", "-c", "copy", "-movflags", "+faststart"],
        )
    finally:
        listing.unlink(missing_ok=True)
