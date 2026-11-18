"""Custom YouTube thumbnails from the template (thumbnails.py).

``thumbnail`` runs after ``upload`` in every job that uploads, while ``youtube_thumbnail`` is on: a thumbnail per
part this job uploaded, from the part file. A re-upload is a new job, so its new videos get theirs too. The
``thumbnail`` kind (``POST /api/v2/vods/{id}/thumbnails``) makes them again for a VOD's existing uploads, from the
VOD's file while it is still on disk, else from a few seconds of each upload downloaded with yt-dlp.

YouTube takes custom thumbnails only from a verified channel, so the channel is checked first. In an upload job
a thumbnail is an extra: a refusal or a failure is logged (and kept in ``payload.thumbnail_failed``), never failing
the job. The ``thumbnail`` kind fails, saying why, when the channel can't have them or no part worked.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import planning, thumbnails
from ..context import JobContext, StepError, StepRefused
from ..youtube import upload_standing
from .metadata import file_chapters, vod_duration
from .previews import _download
from .publish import _publishing_disabled

FRAME_FORMAT = "bv[height<=720]/b[height<=720]/bv/b"  # yt-dlp: as sharp as a 1280x720 thumbnail needs
UNVERIFIED = (
    "YouTube takes custom thumbnails only from a verified channel (long uploads 'allowed'); verify it at "
    "youtube.com/verify"
)


@dataclass
class Target:
    part: int
    youtube_id: str | None  # None: a dry run's, rendered only
    src: Path | None  # the part file, or the VOD's whole one; None: downloaded from YouTube
    seconds: float  # the part's length
    games: list[str]
    start: float = 0.0  # where the part begins in ``src``


def _job_targets(ctx: JobContext, chapters: list[dict[str, Any]] | None) -> list[Target]:
    """The parts this job uploaded (a dry run: every part it cut)."""
    uploaded: dict[str, dict[str, Any]] = ctx.payload.get("uploaded") or {}
    out = []
    for p in ctx.payload.get("parts") or []:
        entry = uploaded.get(str(p["number"]))
        if entry is None and not ctx.settings.dry_run:
            continue
        window = planning.Part(int(p["number"]), float(p["start"]), float(p["end"]))
        games = planning.part_games(chapters, window, ctx.settings.restricted_games)
        out.append(Target(window.number, str(entry["id"]) if entry else None, Path(p["path"]), window.duration, games))
    return out


async def _existing_targets(ctx: JobContext, chapters: list[dict[str, Any]] | None) -> list[Target]:
    """The VOD's uploads of the job's type (``payload.only_parts``: only those parts), framed from the VOD's file
    while it is still on disk, else from YouTube."""
    s = ctx.settings
    vod = await ctx.get_vod()
    try:
        local: Path | None = ctx.default_mp4 if ctx.default_mp4.is_file() else None
    except (KeyError, StepError):  # a live type without its stream id
        local = None
    only = {int(n) for n in ctx.payload.get("only_parts") or []}
    planned = planning.plan_parts(await vod_duration(ctx, vod), chapters, s.restricted_games, s.split_duration)
    windows = {p.number: p for p in planned}
    out = []
    for e in vod.youtube or []:
        number = int(e.get("part") or 1)
        if e.get("type") != ctx.video_type or (only and number not in only):
            continue
        window = windows.get(number)
        games = planning.part_games(chapters, window, s.restricted_games) if window else []
        seconds = float(e.get("duration") or 0) or (window.duration if window else 0.0)
        if local is not None and window is not None:
            out.append(Target(number, str(e["id"]), local, window.duration, games, window.start))
        else:
            out.append(Target(number, str(e["id"]), None, seconds, games))
    return out


async def _channel_refusal(ctx: JobContext) -> str | None:
    yt = ctx.deps.youtube
    channel = await yt.own_channel()
    if channel and (channel.get("status") or {}).get("longUploadsStatus") == "allowed":
        return None
    return f"{UNVERIFIED}. {upload_standing(channel, None)}"


async def thumbnail(ctx: JobContext) -> None:
    s = ctx.settings
    again = ctx.kind == "thumbnail"
    if not again and not s.youtube_thumbnail:
        ctx.log.info("skipping thumbnail: youtube_thumbnail is off")
        return
    if again:
        reason = None if s.youtube_upload else "youtube_upload is off"
        reason = "dry run" if s.dry_run else reason
    else:
        reason = _publishing_disabled(ctx)
    if reason and not s.dry_run:
        ctx.log.info("skipping thumbnail: %s", reason)
        return
    vod = await ctx.get_vod()
    chapters = file_chapters(ctx, vod)
    targets = await _existing_targets(ctx, chapters) if again else _job_targets(ctx, chapters)
    done: dict[str, str] = ctx.payload.setdefault("thumbnails", {})
    targets = [t for t in targets if t.youtube_id is None or done.get(str(t.part)) != t.youtube_id]
    if not targets:
        ctx.log.info("no thumbnails to make")
        return
    if not s.dry_run:
        refusal = await _channel_refusal(ctx)
        if refusal and again:
            raise StepRefused(refusal)
        if refusal:
            ctx.log.warning("skipping thumbnails: %s", refusal)
            ctx.payload["thumbnail_failed"] = {"all": refusal}
            await ctx.save()
            return

    template = await thumbnails.load()
    total = int(ctx.payload.get("total_parts") or 0) or max(
        [int(e.get("part") or 1) for e in vod.youtube or [] if e.get("type") == ctx.video_type] + [len(targets)]
    )
    base = {
        "total": total,
        "label": thumbnails.label_of("custom_upload" if vod.platform == "upload" else ctx.kind, ctx.video_type),
        "title": (vod.title or "").strip(),
        "date": planning.local_date(vod.created_at, s.timezone),
        "channel": s.channel,
    }
    work = ctx.work_dir / "thumbnails"
    failed: dict[str, str] = {}
    for i, t in enumerate(targets):
        ctx.progress(i, len(targets), "parts", f"thumbnail of part {t.part} ({i + 1}/{len(targets)})")
        out = work / f"{ctx.video_type}-{t.part}.jpg"
        values = {**base, "part": t.part, "games": thumbnails.games_text(t.games)}
        try:
            src, at = t.src, t.start + template["at"] * t.seconds
            if src is None and s.dry_run:
                raise RuntimeError("the VOD's file is gone, and a dry run downloads nothing")
            if src is None and t.youtube_id:  # a few seconds from YouTube, from where the frame is taken
                start = int(at)
                src = await _download(ctx, t.youtube_id, work / "src", FRAME_FORMAT, f"*{start}-{start + 3}")
                at = 0.5
            await thumbnails.render(src, at, template, values, out)
            if s.dry_run or t.youtube_id is None:
                ctx.log.info("dry run: thumbnail of part %s in %s", t.part, out)
                continue
            await ctx.deps.youtube.set_thumbnail(t.youtube_id, out)
        except Exception as exc:  # an extra: one part's failure is logged, the rest go on
            ctx.log.warning("thumbnail of part %s failed: %s", t.part, exc)
            failed[str(t.part)] = str(exc)[:500]
            continue
        finally:
            if t.src is None:
                shutil.rmtree(work / "src", ignore_errors=True)
        done[str(t.part)] = t.youtube_id
        out.unlink(missing_ok=True)
        ctx.log.info("thumbnail set on %s (part %s)", t.youtube_id, t.part)
        await ctx.save()
    ctx.progress(len(targets), len(targets), "parts", "thumbnails done")
    if failed:
        ctx.payload["thumbnail_failed"] = failed
        await ctx.save()
        if again and len(failed) == len(targets):
            raise StepError(f"no thumbnail made: {'; '.join(f'part {k}: {v}' for k, v in failed.items())}")
