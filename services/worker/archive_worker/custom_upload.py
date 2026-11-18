"""Custom uploads: a video file sent from Manage in chunks, published to YouTube with no Twitch VOD behind it.

The file arrives in ``<data_dir>/uploads/<id>/`` (``data``, plus ``upload.json``: its name, size, who and when)
through ``PUT`` chunks at the offset it has reached, so a dropped connection resumes where it stopped. Finishing
it checks the file with ffprobe, moves it into the VOD folder and makes a hidden VOD row of the same id
(``upload-`` and 12 hex digits; title from the form, platform ``upload``, no Twitch metadata) with a
``custom_upload`` job (kinds.py): the file is remuxed to the VOD's MP4, the job pauses for the censor
editor and the preview, and once resumed censors, splits, uploads with the form's title, description and
privacy, and cleans up. An upload nobody sent a chunk to for ``custom_upload_abandon_hours`` is deleted
(``sweep``, on every new upload and when the worker starts). The folder is outside ``storage.AREAS``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from . import ffmpeg
from .vods import UPLOAD_PREFIX

log = logging.getLogger(__name__)

PREFIX = UPLOAD_PREFIX
ID = re.compile(r"^upload-[0-9a-f]{12}$")
CHUNK_BYTES = 8 * 1024 * 1024  # one PUT at most: well under the 100 MB a Cloudflare-proxied request may carry
NAME_MAX = 200
TITLE_MAX = 100  # YouTube's limit
DESCRIPTION_MAX = 5000  # YouTube's limit (bytes; checked as characters, and "<" ">" are refused by YouTube)
PRIVACY = ("private", "unlisted", "public")
# What ffprobe calls the containers taken (its format_name lists several names for one family).
FORMATS = {"mov", "mp4", "m4a", "3gp", "matroska", "webm", "mpegts", "flv", "avi"}


class UploadError(Exception):
    def __init__(self, status: int, code: str, msg: str, **extra: Any) -> None:
        super().__init__(msg)
        self.status, self.code, self.msg, self.extra = status, code, msg, extra


def is_upload(vod_id: str | None) -> bool:
    """A VOD made from a custom upload (no Twitch VOD behind it)."""
    return bool(vod_id) and str(vod_id).startswith(PREFIX)


def upload_dir(data_dir: Path) -> Path:
    return data_dir / "uploads"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _folder(data_dir: Path, upload_id: str) -> Path:
    if not ID.match(upload_id):
        raise UploadError(404, "upload_not_found", f"no upload {upload_id}")
    folder = upload_dir(data_dir) / upload_id
    if not (folder / "upload.json").is_file():
        raise UploadError(404, "upload_not_found", f"no upload {upload_id} (finished, cancelled or abandoned)")
    return folder


def status(data_dir: Path, upload_id: str) -> dict[str, Any]:
    """``{id, filename, size_bytes, received_bytes, chunk_bytes, created_at, updated_at, created_by}``."""
    folder = _folder(data_dir, upload_id)
    meta: dict[str, Any] = json.loads((folder / "upload.json").read_text(encoding="utf-8"))
    data = folder / "data"
    st = data.stat()
    updated = dt.datetime.fromtimestamp(st.st_mtime, dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return meta | {"received_bytes": st.st_size, "chunk_bytes": CHUNK_BYTES, "updated_at": updated}


def listing(data_dir: Path) -> list[dict[str, Any]]:
    """The unfinished uploads, newest first (a page that lost its upload finds it here to resume)."""
    base = upload_dir(data_dir)
    if not base.is_dir():
        return []
    out = []
    for folder in base.iterdir():
        try:
            out.append(status(data_dir, folder.name))
        except (UploadError, OSError, ValueError):
            continue
    return sorted(out, key=lambda u: str(u["created_at"]), reverse=True)


def start(data_dir: Path, filename: str, size_bytes: int, max_bytes: int, by: str | None) -> dict[str, Any]:
    name = " ".join(str(filename).split())[:NAME_MAX] or "video"
    if size_bytes <= 0:
        raise UploadError(422, "invalid_upload", "size_bytes must be more than 0")
    if size_bytes > max_bytes:
        raise UploadError(
            413, "too_large", f"the file is {size_bytes / 2**30:.2f} GiB; uploads are at most {max_bytes / 2**30:g} GiB"
        )
    free = shutil.disk_usage(data_dir).free if data_dir.exists() else None
    if free is not None and size_bytes * 2 > free:  # the file, then its remuxed copy
        raise UploadError(507, "disk_full", f"not enough free disk space for a {size_bytes / 2**30:.2f} GiB upload")
    upload_id = PREFIX + secrets.token_hex(6)
    folder = upload_dir(data_dir) / upload_id
    folder.mkdir(parents=True)
    meta = {"id": upload_id, "filename": name, "size_bytes": size_bytes, "created_at": _now(), "created_by": by}
    (folder / "upload.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "data").touch()
    return status(data_dir, upload_id)


_locks: dict[str, asyncio.Lock] = {}


async def write_chunk(data_dir: Path, upload_id: str, offset: int, chunks: Any) -> dict[str, Any]:
    """Append the request body (``chunks``, an async iterator of bytes) at ``offset``, which must be what the
    upload has received so far (409 ``offset_mismatch`` with ``received_bytes`` otherwise: resume from there).
    A body over ``CHUNK_BYTES`` or past the declared size is refused, and what it wrote is cut off again."""
    folder = _folder(data_dir, upload_id)
    lock = _locks.setdefault(upload_id, asyncio.Lock())
    if lock.locked():
        raise UploadError(409, "upload_busy", "another chunk of this upload is being written")
    async with lock:
        meta = status(data_dir, upload_id)
        if offset != meta["received_bytes"]:
            raise UploadError(
                409,
                "offset_mismatch",
                f"the upload has {meta['received_bytes']} bytes; send the chunk from there",
                received_bytes=meta["received_bytes"],
            )
        limit = min(CHUNK_BYTES, meta["size_bytes"] - offset)
        written = 0
        with (folder / "data").open("r+b") as out:
            out.seek(offset)
            try:
                async for piece in chunks:
                    written += len(piece)
                    if written > limit:
                        raise UploadError(
                            413, "chunk_too_large", f"a chunk is at most {limit} bytes here (offset {offset})"
                        )
                    await asyncio.to_thread(out.write, piece)
            except BaseException:
                out.truncate(offset)  # a chunk is all or nothing
                raise
        return status(data_dir, upload_id)


async def probe(path: Path) -> float:
    """The video's length in seconds; UploadError unless ffprobe reads it as a video of a known container."""
    try:
        info = json.loads(
            await ffmpeg.run_probe(
                ["-show_entries", "format=format_name,duration:stream=codec_type,codec_name", "-of", "json", str(path)]
            )
        )
    except (ffmpeg.FfmpegError, ValueError):
        raise UploadError(422, "not_video", "ffprobe could not read the file as a video") from None
    formats = set(str(info.get("format", {}).get("format_name", "")).split(","))
    streams = info.get("streams", [])
    if not formats & FORMATS or not any(
        s.get("codec_type") == "video" and s.get("codec_name") not in ("mjpeg", "png") for s in streams
    ):
        raise UploadError(422, "not_video", f"not a video file ({', '.join(sorted(formats)) or 'unknown format'})")
    try:
        seconds = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise UploadError(422, "not_video", "the video's length can't be read") from None
    if seconds < 1:
        raise UploadError(422, "not_video", "the video is shorter than a second")
    return seconds


def check_details(title: Any, description: Any, privacy: Any) -> tuple[str, str, str]:
    """The form's title, description and privacy, as YouTube takes them."""
    t = " ".join(str(title or "").split())
    if not t:
        raise UploadError(422, "invalid_upload", "a title is required")
    if len(t) > TITLE_MAX:
        raise UploadError(422, "invalid_upload", f"the title is at most {TITLE_MAX} characters")
    d = str(description or "").strip()
    if len(d) > DESCRIPTION_MAX:
        raise UploadError(422, "invalid_upload", f"the description is at most {DESCRIPTION_MAX} characters")
    if any(c in t + d for c in "<>"):
        raise UploadError(422, "invalid_upload", "YouTube refuses < and > in a title or description")
    if privacy not in PRIVACY:
        raise UploadError(422, "invalid_upload", f"privacy must be one of {', '.join(PRIVACY)}")
    return t, d, str(privacy)


async def take(data_dir: Path, upload_id: str, target: Path) -> tuple[dict[str, Any], float]:
    """A complete upload, checked by ffprobe, moved to ``target``: its status and its length in seconds.
    The upload's folder is gone afterwards; a refused file stays, to be cancelled (or replaced) by hand."""
    folder = _folder(data_dir, upload_id)
    lock = _locks.setdefault(upload_id, asyncio.Lock())
    async with lock:
        meta = status(data_dir, upload_id)
        if meta["received_bytes"] != meta["size_bytes"]:
            raise UploadError(
                409,
                "upload_incomplete",
                f"the upload has {meta['received_bytes']} of {meta['size_bytes']} bytes",
                received_bytes=meta["received_bytes"],
            )
        seconds = await probe(folder / "data")
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, folder / "data", target)
        await asyncio.to_thread(shutil.rmtree, folder, True)
    _locks.pop(upload_id, None)
    return meta, seconds


def cancel(data_dir: Path, upload_id: str) -> dict[str, Any]:
    meta = status(data_dir, upload_id)
    shutil.rmtree(upload_dir(data_dir) / upload_id, ignore_errors=True)
    _locks.pop(upload_id, None)
    return meta


def sweep(data_dir: Path, abandon_hours: float) -> list[str]:
    """Delete the uploads nothing was sent to for ``abandon_hours`` (a folder without its ``upload.json``
    counts from its own change time); the ids removed."""
    base = upload_dir(data_dir)
    if not base.is_dir() or abandon_hours <= 0:
        return []
    cutoff = time.time() - abandon_hours * 3600
    gone = []
    for folder in base.iterdir():
        if not ID.match(folder.name) or not folder.is_dir() or (folder.name in _locks and _locks[folder.name].locked()):
            continue
        stamps = [p.stat().st_mtime for p in folder.iterdir()] or [folder.stat().st_mtime]
        if max(stamps) < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            _locks.pop(folder.name, None)
            gone.append(folder.name)
    if gone:
        log.info("removed %d abandoned upload(s): %s", len(gone), ", ".join(gone))
    return gone
