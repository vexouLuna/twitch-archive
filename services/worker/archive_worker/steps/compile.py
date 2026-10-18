"""A synthetic VOD as one file of its own (job kind ``compile``): each of its windows cut from its source's
kept MP4 and joined, then split, uploaded and described like a VOD, as uploads of type ``compiled`` on the
synthetic VOD's row. Uploading needs ``compile_youtube_upload`` on. The segments stay what the VOD is; the
compiled uploads are another way to play it (``play_default``, and the watch page's switcher).

A gap in the synthetic timeline (a playthrough's time between runs) isn't in the file: the windows play back
to back and each upload's ``gaps`` says what it skips, as for a live recording's missed stretches. That needs
no player work; an intermission card in the file would need file time that maps to no VOD time."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from archive_common.db import get_engine, get_sessionmaker
from archive_common.models import Vod
from archive_common.serialize import flat_segments
from archive_common.timeutil import format_hhmmss
from sqlalchemy import select

from .. import ffmpeg, planning, synthetic
from ..context import JobContext, StepRefused


def source_mp4(ctx: JobContext, vod_id: str) -> Path:
    """Where the archive job of ``vod_id`` keeps its MP4 (``keep_mp4``)."""
    return ctx.settings.vod_dir / vod_id / f"{vod_id}.mp4"


async def compile_sources(ctx: JobContext) -> None:
    """The windows to cut (``payload.clips``), each from a kept MP4 of a real VOD, with where each plays
    on the synthetic timeline (``at``). Refused when a source has no kept MP4 or has censor ranges (its MP4
    is the uncensored one), and when a source has DMCA claims, unless ``payload.accept_dmca`` (Manage asks
    first, listing them: synthetic.dmca_claims)."""
    if ctx.payload.get("clips"):
        return
    vod = await ctx.get_vod()
    if vod.synthetic is None:
        raise StepRefused(f"{vod.id} is not a synthetic VOD; only those are compiled")
    async with get_engine().connect() as conn:
        segs = [x for x in (await flat_segments(conn, [vod.id])).get(vod.id, []) if x.length > 0]
    if not segs:
        raise StepRefused(f"{vod.id} has no segments to compile")
    ids = sorted({x.source_id for x in segs})
    async with get_sessionmaker()() as s:
        censored = (await s.execute(select(Vod.id).where(Vod.id.in_(ids), Vod.censor != []))).scalars().all()
    if censored:
        raise StepRefused(f"source(s) {', '.join(censored)} have censor ranges, which their kept MP4 doesn't have")
    missing = [i for i in ids if not source_mp4(ctx, i).exists()]
    if missing:
        raise StepRefused(f"no kept MP4 of source(s) {', '.join(missing)} (keep_mp4 on, then archive them again)")
    found = await synthetic.dmca_claims(vod.id)
    claims = found["overlapping"] + found["other"]  # any claim needs accepting, also one outside the windows
    if claims and not ctx.payload.get("accept_dmca"):
        listed = ", ".join(sorted({c["vod_id"] for c in claims}))
        raise StepRefused(f"source(s) {listed} have DMCA claims; compile from Manage and accept the risks")
    ctx.payload["compiled_from"] = (vod.synthetic or {}).get("changedAt")  # see compiled()
    ctx.payload["clips"] = [
        {"vod_id": x.source_id, "start": x.start, "end": x.end, "at": x.at, "path": str(source_mp4(ctx, x.source_id))}
        for x in segs
    ]
    await ctx.save()


async def compile(ctx: JobContext) -> None:
    """Cut each clip and join them into the job's MP4 (kept if already done), and save where each clip is in
    the file (``payload.timeline``, planning.recording_timeline's shape on the synthetic VOD's time) for the
    chapters and each upload's ``gaps``. Each window is re-encoded, so it starts and ends on the exact
    frame, and converted to the sources' largest resolution and highest frame rate (``payload.video``,
    planning.video_target) so they join; its measured length is still used."""
    out = ctx.default_mp4
    if out.exists() and ctx.payload.get("duration"):
        return
    clips = ctx.payload["clips"]
    work = ctx.work_dir / "compile"
    if not ctx.payload.get("video"):  # saved, so a re-run part-way cuts the rest to the same size
        found = [await ffmpeg.probe_video(Path(p)) for p in sorted({c["path"] for c in clips})]
        ctx.payload["video"] = list(planning.video_target(found))
        await ctx.save()
    w, h, rate = ctx.payload["video"]
    paths, timeline, rec = [], [], 0.0
    for i, c in enumerate(clips):
        path = work / f"{i:04d}.mp4"
        if not path.exists():  # a finished cut survives a crash part-way through
            await ffmpeg.encode(Path(c["path"]), path, c["start"], c["end"] - c["start"], (w, h, rate))
        paths.append(path)
        length = await ffmpeg.probe_duration(path)
        timeline.append([round(rec, 3), round(float(c["at"]), 3), round(length, 3)])
        rec += length
        ctx.progress(i + 1, len(clips), "parts", f"cut {i + 1}/{len(clips)} windows")
    ctx.log.info("joining %d windows at %dx%d %s fps into %s", len(paths), w, h, rate, out)
    await ffmpeg.concat(paths, out)
    await asyncio.to_thread(shutil.rmtree, work, True)
    ctx.payload["duration"] = await ffmpeg.probe_duration(out)
    ctx.payload["timeline"] = timeline
    ctx.payload.pop("parts", None)  # split cuts the new file
    await ctx.save()
    ctx.log.info("compiled %s (%s)", out, format_hhmmss(ctx.payload["duration"]))


async def compiled(ctx: JobContext) -> None:
    """Once uploaded, note which version of the segments they are (``synthetic.compiledFrom``, the
    ``changedAt`` compile_sources saw): when the segments change after, Manage shows them outdated."""
    if not ctx.payload.get("uploaded"):
        return
    async with get_sessionmaker()() as s, s.begin():
        vod = (await s.execute(select(Vod).where(Vod.id == ctx.vod_id).with_for_update())).scalar_one()
        vod.synthetic = {**(vod.synthetic or {}), "compiledFrom": ctx.payload.get("compiled_from")}
