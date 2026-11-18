"""The censor song library: short audio files a censor range can loop over what it mutes
(``vods.censor[].audio = "song"``, ``song`` = an id from here).

Each song is ``<data_dir>/songs/<id>.m4a`` (what was uploaded, decoded and encoded again as AAC, so a
file that only claims to be audio never gets in) beside ``<id>.json`` (its name, length, size, when and who).
The folder is outside ``storage.AREAS``, so the storage view never offers it for cleanup. An id is never
reused: a range naming a deleted song fails its censor step with that id.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from pathlib import Path
from typing import Any

from . import ffmpeg

ID = "song-"  # + 12 hex digits
MAX_BYTES = 25 * 1024 * 1024  # an upload, before re-encoding
MAX_SECONDS = 600.0  # longer is cut to this: a song loops anyway
MIN_SECONDS = 0.5
NAME_MAX = 80
# What ffprobe calls the containers taken (its format_name, which may list several names).
FORMATS = {"mp3", "mov", "mp4", "m4a", "ogg", "wav", "flac", "matroska", "webm", "aac", "opus"}


class SongError(Exception):
    def __init__(self, status: int, code: str, msg: str) -> None:
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


def song_dir(data_dir: Path) -> Path:
    return data_dir / "songs"


def is_id(value: str) -> bool:
    rest = value.removeprefix(ID)
    return value.startswith(ID) and len(rest) == 12 and all(c in "0123456789abcdef" for c in rest)


def path(data_dir: Path, song_id: str) -> Path | None:
    """The song's audio file, or None if there is no such song."""
    if not is_id(song_id):
        return None
    file = song_dir(data_dir) / f"{song_id}.m4a"
    return file if file.is_file() else None


def _meta(file: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) and (file.with_suffix(".m4a")).is_file() else None


def listing(data_dir: Path) -> list[dict[str, Any]]:
    """Every song, newest first: ``{id, name, duration_seconds, size_bytes, created_at, created_by}``."""
    folder = song_dir(data_dir)
    if not folder.is_dir():
        return []
    songs = [m for f in folder.glob(f"{ID}*.json") if is_id(f.stem) and (m := _meta(f))]
    return sorted(songs, key=lambda m: str(m.get("created_at")), reverse=True)


def ids(data_dir: Path) -> set[str]:
    return {m["id"] for m in listing(data_dir)}


def clean_name(name: str | None) -> str:
    text = " ".join((name or "").split())
    if not text:
        raise SongError(422, "invalid_song", "a song needs a name")
    if len(text) > NAME_MAX:
        raise SongError(422, "invalid_song", f"a song's name is at most {NAME_MAX} characters")
    return text


async def probe(file: Path) -> float:
    """The audio's length in seconds; SongError unless ffprobe reads it as an audio file of a known container."""
    try:
        out = await ffmpeg.run_probe(
            ["-show_entries", "format=format_name,duration:stream=codec_type", "-of", "json", str(file)]
        )
        info = json.loads(out)
    except (ffmpeg.FfmpegError, ValueError):
        raise SongError(422, "not_audio", "ffprobe could not read the file as audio") from None
    formats = set(str(info.get("format", {}).get("format_name", "")).split(","))
    if not formats & FORMATS:
        raise SongError(422, "not_audio", f"not an audio file this library takes ({', '.join(sorted(FORMATS))})")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise SongError(422, "not_audio", "the file has no audio stream")
    try:
        seconds = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise SongError(422, "not_audio", "the file's length can't be read") from None
    if seconds < MIN_SECONDS:
        raise SongError(422, "not_audio", f"a song must be at least {MIN_SECONDS:g} s long")
    return seconds


async def add(data_dir: Path, upload: Path, name: str, by: str | None) -> dict[str, Any]:
    """Check ``upload`` (a file the caller wrote, removed here either way), store it re-encoded, return its entry."""
    try:
        name = clean_name(name)
        await probe(upload)
        folder = song_dir(data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        song_id = ID + secrets.token_hex(6)
        out = folder / f"{song_id}.m4a"
        try:
            await ffmpeg.encode_audio(upload, out, MAX_SECONDS)
        except ffmpeg.FfmpegError:
            raise SongError(422, "not_audio", "ffmpeg could not decode the audio") from None
        meta = {
            "id": song_id,
            "name": name,
            "duration_seconds": round(await ffmpeg.probe_duration(out), 3),
            "size_bytes": out.stat().st_size,
            "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "created_by": by,
        }
        (folder / f"{song_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta
    finally:
        upload.unlink(missing_ok=True)


def delete(data_dir: Path, song_id: str) -> dict[str, Any]:
    folder = song_dir(data_dir)
    meta = _meta(folder / f"{song_id}.json") if is_id(song_id) else None
    if meta is None:
        raise SongError(404, "song_not_found", f"no song {song_id}")
    (folder / f"{song_id}.json").unlink(missing_ok=True)
    (folder / f"{song_id}.m4a").unlink(missing_ok=True)
    return meta
