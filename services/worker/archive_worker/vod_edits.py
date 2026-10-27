"""Validation for hand edits of a VOD (admin API): its fields (title, hidden, thumbnail,
duration, date), chapters, YouTube and Drive lists.

Pure functions: each takes the request's list and returns what to store, or
raises ValueError with a message fit for the response.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Collection
from typing import Any
from urllib.parse import urlsplit

from archive_common.serialize import box_art_image
from archive_common.timeutil import format_hhmmss, hhmmss_to_seconds

from . import planning

VIDEO_TYPES = ("vod", "live")
GAP_KIND = "gap"  # chapters[].kind: marks a merge's gap (see timeline); the only kind, else absent
OVERLAP_SLACK = 0.001  # seconds of float noise tolerated where chapters meet
DURATION_SLACK = 1.0  # stored durations are whole seconds (HH:MM:SS), so allow the lost fraction


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _list(items: Any, name: str) -> list[Any]:
    if not isinstance(items, list):
        raise ValueError(f"{name} must be a list")
    return items


def _object(item: Any, where: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:  # type: ignore[assignment]
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be an object")
    missing = sorted(required - item.keys())
    if missing:
        raise ValueError(f"{where} is missing {', '.join(missing)}")
    unknown = sorted(item.keys() - required - optional)
    if unknown:
        raise ValueError(f"{where} has unknown field(s) {', '.join(unknown)}")
    return item


def _optional_str(item: dict[str, Any], key: str, where: str) -> str | None:
    value = item.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}.{key} must be a string or null")
    return value


def chapters(items: Any, duration: float) -> list[dict[str, Any]]:
    """Admin chapters -> the stored shape (see planning): sorted by start, no overlaps,
    every length > 0, and inside ``duration`` seconds (unchecked when that is 0/unknown).
    ``kind`` is kept when given (a gap chapter stays one)."""
    out: list[dict[str, Any]] = []
    prev_start = prev_end = None
    for i, item in enumerate(_list(items, "chapters")):
        where = f"chapters[{i}]"
        ch = _object(item, where, {"name", "gameId", "start", "length", "restricted"}, {"imageTemplate", "kind"})
        name = _optional_str(ch, "name", where)
        game_id = _optional_str(ch, "gameId", where)  # Helix ids are strings
        template = _optional_str(ch, "imageTemplate", where)
        start, length = ch["start"], ch["length"]
        if not is_number(start) or start < 0:
            raise ValueError(f"{where}.start must be a number of seconds >= 0")
        if not is_number(length) or length <= 0:
            raise ValueError(f"{where}.length must be a number of seconds > 0")
        if not isinstance(ch["restricted"], bool):
            raise ValueError(f"{where}.restricted must be true or false")
        kind = ch.get("kind")
        if kind not in (None, GAP_KIND):
            raise ValueError(f"{where}.kind must be {GAP_KIND!r} or absent")
        if prev_start is not None and start < prev_start:
            raise ValueError(f"{where} starts before chapters[{i - 1}]; sort chapters by start")
        if prev_end is not None and start < prev_end - OVERLAP_SLACK:
            raise ValueError(f"{where} starts at {start}s, inside chapters[{i - 1}] (which ends at {prev_end}s)")
        if duration > 0 and start + length > duration + DURATION_SLACK:
            raise ValueError(f"{where} ends at {start + length}s, after the end of the VOD ({duration:g}s)")
        prev_start, prev_end = start, start + length
        out.append(
            {
                **planning.chapter(game_id, name, box_art_image(template), start, length, ch["restricted"]),
                "imageTemplate": template,
                **({"kind": kind} if kind is not None else {}),
            }
        )
    return out


def _video_type(item: dict[str, Any], where: str) -> str:
    if item["type"] not in VIDEO_TYPES:
        raise ValueError(f"{where}.type must be 'vod' or 'live'")
    return item["type"]  # type: ignore[no-any-return]


def _video_id(item: dict[str, Any], where: str) -> str:
    if not isinstance(item["id"], str) or not item["id"].strip():
        raise ValueError(f"{where}.id must be a non-empty string")
    return item["id"].strip()


def youtube(items: Any, existing: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Admin YouTube list -> vods.youtube. A video already listed keeps its thumbnail, seek-bar
    previews and recording gaps (and its duration, unless a new one is given)."""
    before = {e.get("id"): e for e in existing or [] if isinstance(e, dict)}
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_parts: set[tuple[str, int]] = set()
    for i, item in enumerate(_list(items, "youtube")):
        where = f"youtube[{i}]"
        _object(item, where, {"id", "type", "part"}, {"duration"})
        video_id, typ = _video_id(item, where), _video_type(item, where)
        part = item["part"]
        if not isinstance(part, int) or isinstance(part, bool) or part < 1:
            raise ValueError(f"{where}.part must be a whole number >= 1")
        if video_id in seen_ids:
            raise ValueError(f"{where}: video {video_id} is listed twice")
        if (typ, part) in seen_parts:
            raise ValueError(f"{where}: there is already a {typ} part {part}")
        seen_ids.add(video_id)
        seen_parts.add((typ, part))
        old = before.get(video_id, {})
        entry: dict[str, Any] = {"id": video_id, "type": typ}
        duration = item.get("duration", old.get("duration"))
        if duration is not None:
            if not is_number(duration) or duration < 0:
                raise ValueError(f"{where}.duration must be a number of seconds >= 0")
            entry["duration"] = planning.num_seconds(duration)
        entry["part"] = part
        entry["thumbnail_url"] = old.get("thumbnail_url") or planning.youtube_thumbnail(video_id)
        if old.get("preview"):
            entry["preview"] = old["preview"]
        if old.get("gaps") and typ == old.get("type"):
            entry["gaps"] = old["gaps"]
        out.append(entry)
    return out


def drive(items: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, item in enumerate(_list(items, "drive")):
        where = f"drive[{i}]"
        _object(item, where, {"id", "type"})
        file_id, typ = _video_id(item, where), _video_type(item, where)
        if file_id in seen:
            raise ValueError(f"{where}: file {file_id} is listed twice")
        seen.add(file_id)
        out.append({"id": file_id, "type": typ})
    return out


CENSOR_VIDEO = ("blur", "black", "pixelate", "color")
NOTE_MAX = 200
REGION_MIN = 0.01  # a box's smallest width or height, as a fraction of the frame
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _region(value: Any, where: str) -> dict[str, float]:
    """``{x, y, w, h}``: fractions of the frame from its top-left corner, rounded to 4 places, inside it."""
    r = _object(value, where, {"x", "y", "w", "h"})
    if not all(is_number(r[k]) for k in "xywh"):
        raise ValueError(f"{where}: x, y, w and h must be numbers")
    x, y, w, h = (round(float(r[k]), 4) for k in "xywh")
    if x < 0 or y < 0:
        raise ValueError(f"{where}: x and y must be 0 or more")
    if w < REGION_MIN or h < REGION_MIN:
        raise ValueError(f"{where}: w and h must be at least {REGION_MIN:g}")
    if x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
        raise ValueError(f"{where} reaches past the edge of the frame")
    return {"x": x, "y": y, "w": w, "h": h}


def censor(items: Any, duration: float) -> list[dict[str, Any]]:
    """Admin censor ranges -> ``vods.censor``: ``{start, end, video, color, region, mute, note}`` in VOD
    seconds, sorted by start, each covering the video (``video``: blurred, blacked out, pixelated or filled
    with ``color``, the whole frame or only a ``region`` box), muting the audio (``mute``) or both, and
    inside ``duration`` (unchecked when that is 0/unknown). Ranges may overlap: the step merges them."""
    out: list[dict[str, Any]] = []
    for i, item in enumerate(_list(items, "censor")):
        where = f"censor[{i}]"
        r = _object(item, where, {"start", "end"}, {"video", "color", "region", "mute", "note"})
        start, end = r["start"], r["end"]
        if not is_number(start) or start < 0:
            raise ValueError(f"{where}.start must be a number of seconds >= 0")
        if not is_number(end) or end <= start:
            raise ValueError(f"{where}.end must be a number of seconds after its start")
        if duration > 0 and end > duration + DURATION_SLACK:
            raise ValueError(f"{where} ends at {end:g}s, after the end of the VOD ({duration:g}s)")
        video = r.get("video")
        if video not in (None, *CENSOR_VIDEO):
            raise ValueError(f"{where}.video must be {', '.join(map(repr, CENSOR_VIDEO))} or null")
        color = r.get("color")
        if video == "color":
            if not isinstance(color, str) or not _COLOR.match(color):
                raise ValueError(f"{where}.color must be #rrggbb")
            color = color.lower()
        elif color is not None:
            raise ValueError(f"{where}.color is only for video 'color'")
        region = None if r.get("region") is None else _region(r["region"], f"{where}.region")
        if region is not None and video is None:
            raise ValueError(f"{where}.region needs a video cover")
        mute = r.get("mute", False)
        if not isinstance(mute, bool):
            raise ValueError(f"{where}.mute must be true or false")
        if video is None and not mute:
            raise ValueError(f"{where} neither covers the video nor mutes the audio")
        note = (_optional_str(r, "note", where) or "").strip() or None
        if note is not None and len(note) > NOTE_MAX:
            raise ValueError(f"{where}.note is longer than {NOTE_MAX} characters")
        out.append(
            {"start": planning.num_seconds(start), "end": planning.num_seconds(end), "video": video}
            | ({"color": color} if color else {})
            | ({"region": region} if region else {})
            | {"mute": mute}
            | ({"note": note} if note else {})
        )
    return sorted(out, key=lambda r: (r["start"], r["end"]))


# ── VOD fields (PATCH /admin/vods/{id}) ──────────────────────────────────

# Request key -> vods column. A merged VOD takes only MERGED_EDITABLE (its content is the other VOD's now).
FIELDS = {
    "title": "title",
    "hidden": "hidden",
    "thumbnailUrl": "thumbnail_url",
    "duration": "duration",
    "createdAt": "created_at",
    "tags": "tags",
    "cutReason": "cut_reason",
}
MERGED_EDITABLE = {"hidden", "tags"}
# A synthetic VOD's duration, chapters and date are composed from its segments; its thumbnail too, unless
# it was given its own (synthetic.thumbnailUrl).
SYNTHETIC_EDITABLE = {"title", "hidden", "thumbnailUrl", "tags", "cutReason"}
# vods.tags: an untagged VOD is a regular one; a tagged one is listed apart (the site gives each known
# tag its own tab). The known tags are the site's list (site_tags.vod_tags); these until it is saved.
KNOWN_TAGS = ("compilation", "playthroughs", "vods")  # while the site's tag list was never saved
CUT_REASON_MAX = 500
_HHMMSS = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


def http_url(value: Any, where: str) -> str | None:
    """An http(s) URL, or None for null / empty."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a URL or null")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{where} must be an http(s) URL or null")
    return value.strip()


def duration(value: Any) -> str:
    """``HH:MM:SS`` (hours may run past 99), stored zero-padded."""
    match = _HHMMSS.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("duration must be HH:MM:SS")
    return format_hhmmss(hhmmss_to_seconds(value))


def created_at(value: Any) -> dt.datetime:
    """An ISO 8601 date and time with its offset (``Z`` or ``+hh:mm``)."""
    try:
        when = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        raise ValueError("createdAt must be an ISO date and time with an offset, e.g. 2026-09-30T18:00:00Z")
    return when.astimezone(dt.UTC)


def vod_fields(body: dict[str, Any], known_tags: Collection[str] = KNOWN_TAGS) -> dict[str, Any]:
    """PATCH body -> the vods columns to set. Unknown keys are refused."""
    unknown = sorted(set(body) - FIELDS.keys())
    if unknown:
        raise ValueError(f"unknown field(s) {', '.join(unknown)}; these can be changed: {', '.join(FIELDS)}")
    out: dict[str, Any] = {}
    if "title" in body:
        if not isinstance(body["title"], str) or not body["title"].strip():
            raise ValueError("title must be a non-empty string")
        out["title"] = body["title"].strip()
    if "hidden" in body:
        if not isinstance(body["hidden"], bool):
            raise ValueError("hidden must be true or false")
        out["hidden"] = body["hidden"]
    if "thumbnailUrl" in body:
        out["thumbnail_url"] = http_url(body["thumbnailUrl"], "thumbnailUrl")
    if "duration" in body:
        out["duration"] = duration(body["duration"])
    if "createdAt" in body:
        out["created_at"] = created_at(body["createdAt"])
    if "tags" in body:
        out["tags"] = tags(body["tags"], known_tags)
    if "cutReason" in body:
        out["cut_reason"] = cut_reason(body["cutReason"])
    return out


def cut_reason(value: Any) -> str | None:
    """Free text up to CUT_REASON_MAX characters, trimmed; None for null / blank."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("cutReason must be text or null")
    text = value.strip()
    if len(text) > CUT_REASON_MAX:
        raise ValueError(f"cutReason must be at most {CUT_REASON_MAX} characters")
    return text or None


def tags(value: Any, known: Collection[str] = KNOWN_TAGS) -> list[str]:
    """Tags of ``known``, each once, sorted ([] = a regular VOD)."""
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ValueError("tags must be a list of strings")
    out = sorted({t.strip().lower() for t in value})
    unknown = [t for t in out if t not in known]
    if unknown:
        raise ValueError(f"unknown tag(s) {', '.join(unknown)}; known: {', '.join(sorted(known))}")
    return out


def check_fits(chapters: Any, seconds: float) -> None:
    """A new duration must still hold the VOD's chapters (ValueError otherwise)."""
    end = content_end(chapters)
    if end > seconds + DURATION_SLACK:
        raise ValueError(f"The chapters run to {end:g}s, past the new duration ({seconds:g}s); shorten them first")


def content_end(chapters: Any) -> float:
    """Where the last chapter ends, in seconds (``end`` holds each chapter's length)."""
    ends = [
        c["start"] + c["end"]
        for c in chapters or []
        if isinstance(c, dict) and is_number(c.get("start")) and is_number(c.get("end"))
    ]
    return max(ends, default=0)
