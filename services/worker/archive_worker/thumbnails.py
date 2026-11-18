"""YouTube thumbnails from one template: a frame of the part with lines of text drawn over it (steps/thumbnail.py).

The template is a contract between this worker and Manage's thumbnail editor (docs/admin-api.md, "Thumbnails"),
stored as JSON in ``app_state`` (key ``thumbnail_template``)::

    {
      "at": 0.25,                       # where in the part the frame is taken, 0 to 1 of its length
      "lines": [                        # at most MAX_LINES, drawn in order (a later one over an earlier one)
        {
          "text": "PART {part}/{total}",  # at most TEXT_MAX characters; {field}s from FIELDS, no other braces
          "x": 0.04, "y": 0.06,           # 0 to 1 of the frame: the line's top, and its left / centre / right
          "align": "left",                # left | center | right: which edge of the text sits at x
          "size": 0.08,                   # text height, 0.02 to 0.3 of the frame's
          "color": "#ffffff",             # the text's colour
          "box": "#000000"                # a box behind the text in this colour (85% opaque), or null for an outline
        }
      ]
    }

A line whose text is empty once filled in (a part with no games, say) is left out. Text too wide for the room on
its side of ``x`` is made smaller until it fits. The picture is 1280x720 JPEG, at most 2 MB, as YouTube takes it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from archive_common.db import get_sessionmaker
from archive_common.models import AppState
from sqlalchemy.dialects.postgresql import insert

from . import ffmpeg

STATE_KEY = "thumbnail_template"
WIDTH, HEIGHT = 1280, 720
MAX_BYTES = 2 * 2**20  # YouTube's limit
MAX_LINES = 8
TEXT_MAX = 120
GAMES_MAX = 60  # characters of the games list before it is cut with "…"
FIELDS = ("part", "total", "games", "label", "title", "date", "channel")
ALIGNS = ("left", "center", "right")
LABELS = {"live": "Live", "vod": "VOD", "compiled": "Compilation"}
DEFAULT: dict[str, Any] = {
    "at": 0.25,
    "lines": [
        {"text": "{label}", "x": 0.04, "y": 0.06, "align": "left", "size": 0.07, "color": "#ffffff", "box": "#9146ff"},
        {
            "text": "PART {part}/{total}",
            "x": 0.96,
            "y": 0.06,
            "align": "right",
            "size": 0.07,
            "color": "#ffffff",
            "box": "#000000",
        },
        {"text": "{games}", "x": 0.5, "y": 0.8, "align": "center", "size": 0.08, "color": "#ffffff", "box": "#000000"},
    ],
}
_FIELD = re.compile(r"\{(\w+)\}")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_QUALITIES = (3, 6, 10, 16, 24)  # -q:v tried in turn until the JPEG is small enough


class TemplateError(ValueError):
    pass


def _fraction(line: dict[str, Any], key: str, lo: float, hi: float, where: str) -> float:
    v = line.get(key)
    if isinstance(v, bool) or not isinstance(v, int | float) or not lo <= v <= hi:
        raise TemplateError(f"{where}{key} must be a number from {lo:g} to {hi:g}")
    return round(float(v), 4)


def check(raw: Any) -> dict[str, Any]:
    """The template as stored, or TemplateError saying what is wrong with it."""
    if not isinstance(raw, dict):
        raise TemplateError("the template must be an object with at and lines")
    extra = set(raw) - {"at", "lines"}
    if extra:
        raise TemplateError(f"unknown template keys: {', '.join(sorted(extra))}")
    at = _fraction(raw, "at", 0, 1, "")
    lines = raw.get("lines")
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise TemplateError(f"lines must be a list of at most {MAX_LINES}")
    out = []
    for i, line in enumerate(lines):
        where = f"line {i + 1}: "
        if not isinstance(line, dict):
            raise TemplateError(f"{where}must be an object")
        extra = set(line) - {"text", "x", "y", "align", "size", "color", "box"}
        if extra:
            raise TemplateError(f"{where}unknown keys: {', '.join(sorted(extra))}")
        text = line.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX or _CONTROL.search(text):
            raise TemplateError(f"{where}text must be 1 to {TEXT_MAX} characters on one line")
        unknown = [f for f in _FIELD.findall(text) if f not in FIELDS]
        if unknown:
            raise TemplateError(f"{where}unknown field {{{unknown[0]}}}; fields: {', '.join(FIELDS)}")
        if "{" in _FIELD.sub("", text) or "}" in _FIELD.sub("", text):
            raise TemplateError(f"{where}braces are only for fields, like {{part}}")
        align = line.get("align", "left")
        if align not in ALIGNS:
            raise TemplateError(f"{where}align must be one of {', '.join(ALIGNS)}")
        color, box = line.get("color", "#ffffff"), line.get("box")
        if not isinstance(color, str) or not _COLOR.fullmatch(color):
            raise TemplateError(f"{where}color must be #rrggbb")
        if box is not None and (not isinstance(box, str) or not _COLOR.fullmatch(box)):
            raise TemplateError(f"{where}box must be #rrggbb or null")
        out.append(
            {
                "text": " ".join(text.split()),
                "x": _fraction(line, "x", 0, 1, where),
                "y": _fraction(line, "y", 0, 1, where),
                "align": align,
                "size": _fraction(line, "size", 0.02, 0.3, where),
                "color": color.lower(),
                "box": box.lower() if box else None,
            }
        )
    return {"at": at, "lines": out}


async def load() -> dict[str, Any]:
    """The saved template, or DEFAULT while none was saved (or the saved one no longer passes ``check``)."""
    async with get_sessionmaker()() as s:
        row = await s.get(AppState, STATE_KEY)
    if row is None:
        return DEFAULT
    try:
        return check(row.value)
    except TemplateError:
        return DEFAULT


async def saved() -> bool:
    async with get_sessionmaker()() as s:
        return await s.get(AppState, STATE_KEY) is not None


async def save(template: dict[str, Any]) -> None:
    async with get_sessionmaker()() as s:
        stmt = insert(AppState).values(key=STATE_KEY, value=template)
        await s.execute(stmt.on_conflict_do_update(index_elements=[AppState.key], set_={"value": template}))
        await s.commit()


def label_of(kind: str, video_type: str) -> str:
    """The Live/VOD label: what the upload is a copy of (a custom upload: "Upload")."""
    return "Upload" if kind == "custom_upload" else LABELS.get(video_type, "VOD")


def games_text(names: list[str]) -> str:
    text = " · ".join(names)
    return text if len(text) <= GAMES_MAX else text[: GAMES_MAX - 1].rstrip(" ·") + "…"


def fill(text: str, values: dict[str, Any]) -> str:
    """``text`` with its fields filled in from ``values`` (missing ones empty), on one line."""
    filled = _FIELD.sub(lambda m: str(values.get(m.group(1), "")), text)
    return " ".join(_CONTROL.sub(" ", filled).split())


def _drawtext(line: dict[str, Any], text_file: Path, chars: int) -> str:
    x, y = line["x"] * WIDTH, line["y"] * HEIGHT
    room = {"left": WIDTH - x, "right": x, "center": 2 * min(x, WIDTH - x)}[line["align"]] - 16
    size = int(max(10, min(line["size"] * HEIGHT, room / (0.62 * max(chars, 1)))))  # 0.62 em: DejaVu Bold's width
    left = {"left": f"{x:.0f}", "right": f"{x:.0f}-text_w", "center": f"{x:.0f}-text_w/2"}[line["align"]]
    color = "0x" + line["color"][1:]
    look = (
        f":box=1:boxcolor=0x{line['box'][1:]}@0.85:boxborderw={max(4, size // 4)}"
        if line["box"]
        else f":borderw={max(1, size // 14)}:bordercolor=black"
    )
    return (
        f"drawtext=fontfile={ffmpeg._quoted(Path(ffmpeg.CENSOR_FONT))}:textfile={ffmpeg._quoted(text_file)}"
        f":expansion=none:fontsize={size}:fontcolor={color}{look}:x={left}:y={y:.0f}"
    )


def filter_graph(template: dict[str, Any], values: dict[str, Any], work: Path) -> str:
    """The ``-vf`` for one thumbnail: the frame fitted into 1280x720 (bars where it isn't 16:9), then each line.
    Each line's text goes in its own file in ``work``, read with expansion off, so nothing in it is ffmpeg syntax."""
    graph = [
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease",
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
    ]
    work.mkdir(parents=True, exist_ok=True)
    for i, line in enumerate(template["lines"]):
        text = fill(line["text"], values)
        if not text:
            continue
        path = work / f"line-{i}.txt"
        path.write_text(text, encoding="utf-8")
        graph.append(_drawtext(line, path, len(text)))
    return ",".join(graph)


async def render(
    src: Path | None, at_seconds: float, template: dict[str, Any], values: dict[str, Any], out: Path
) -> Path:
    """The thumbnail of ``src``'s frame at ``at_seconds`` as the JPEG ``out``, made smaller (lower quality)
    until it is within MAX_BYTES. The line files go in a folder beside ``out``, removed after."""
    work = out.with_name(out.stem + "-lines")
    try:
        vf = filter_graph(template, values, work)
        for quality in _QUALITIES:
            await ffmpeg.still(src, at_seconds, out, vf, quality)
            if out.stat().st_size <= MAX_BYTES:
                return out
        raise ffmpeg.FfmpegError(f"the thumbnail is still {out.stat().st_size} bytes at the lowest quality")
    finally:
        for f in work.glob("line-*.txt"):
            f.unlink(missing_ok=True)
        try:
            work.rmdir()
        except OSError:
            pass
