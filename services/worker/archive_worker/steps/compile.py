"""A synthetic VOD as one file of its own (job kind ``compile``): each of its windows cut from its source's
kept MP4 and joined, then split, uploaded and described like a VOD, as uploads of type ``compiled`` on the
synthetic VOD's row. Uploading needs ``compile_upload`` on. The segments stay what the VOD is; the compiled
uploads are another way to play it (``play_default``, and the watch page's switcher)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from archive_common.db import get_engine, get_sessionmaker
from archive_common.models import Vod
from archive_common.serialize import flat_segments
from archive_common.timeutil import format_hhmmss
from sqlalchemy import select

from .. import ffmpeg
from ..context import JobContext, StepRefused

GAP = 1.0  # seconds of the synthetic timeline that may play nothing between two windows


def source_mp4(ctx: JobContext, vod_id: str) -> Path:
    """Where the archive job of ``vod_id`` keeps its MP4 (``keep_mp4``)."""
    return ctx.settings.vod_dir / vod_id / f"{vod_id}.mp4"


async def compile_sources(ctx: JobContext) -> None:
    """The windows to cut (``payload.clips``), each from a kept MP4 of a real VOD. Refused when a source has
    no kept MP4, has censor ranges (its MP4 is the uncensored one), or the timeline has a gap."""
    if ctx.payload.get("clips"):
        return
    vod = await ctx.get_vod()
    if vod.synthetic is None:
        raise StepRefused(f"{vod.id} is not a synthetic VOD; only those are compiled")
    async with get_engine().connect() as conn:
        segs = [x for x in (await flat_segments(conn, [vod.id])).get(vod.id, []) if x.length > 0]
    if not segs:
        raise StepRefused(f"{vod.id} has no segments to compile")
    end = 0.0
    for x in segs:
        # shortcut: a gap is refused, not filled with black; render a filler clip once merges with gaps need it.
        if x.at - end > GAP:
            raise StepRefused(
                f"{format_hhmmss(end)}-{format_hhmmss(x.at)} plays nothing; a compiled file can't hold a gap"
            )
        end = x.at + x.length
    ids = sorted({x.source_id for x in segs})
    async with get_sessionmaker()() as s:
        censored = (await s.execute(select(Vod.id).where(Vod.id.in_(ids), Vod.censor != []))).scalars().all()
    if censored:
        raise StepRefused(f"source(s) {', '.join(censored)} have censor ranges, which their kept MP4 doesn't have")
    missing = [i for i in ids if not source_mp4(ctx, i).exists()]
    if missing:
        raise StepRefused(f"no kept MP4 of source(s) {', '.join(missing)} (keep_mp4 on, then archive them again)")
    ctx.payload["clips"] = [
        {"vod_id": x.source_id, "start": x.start, "end": x.end, "path": str(source_mp4(ctx, x.source_id))} for x in segs
    ]
    await ctx.save()


async def compile(ctx: JobContext) -> None:
    """Cut each clip and join them into the job's MP4 (kept if already done). Stream copy cuts at keyframes,
    so each join can move what follows by up to a keyframe interval (2 s on Twitch)."""
    out = ctx.default_mp4
    if out.exists() and ctx.payload.get("duration"):
        return
    clips = ctx.payload["clips"]
    work = ctx.work_dir / "compile"
    paths = []
    for i, c in enumerate(clips):
        path = work / f"{i:04d}.mp4"
        if not path.exists():  # a finished cut survives a crash part-way through
            await ffmpeg.cut(Path(c["path"]), path, c["start"], c["end"] - c["start"], faststart=False)
        paths.append(path)
        ctx.progress(i + 1, len(clips), "parts", f"cut {i + 1}/{len(clips)} windows")
    ctx.log.info("joining %d windows into %s", len(paths), out)
    await ffmpeg.concat(paths, out)
    await asyncio.to_thread(shutil.rmtree, work, True)
    ctx.payload["duration"] = await ffmpeg.probe_duration(out)
    ctx.payload.pop("parts", None)  # split cuts the new file
    await ctx.save()
    ctx.log.info("compiled %s (%s)", out, format_hhmmss(ctx.payload["duration"]))
