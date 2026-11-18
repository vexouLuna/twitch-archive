"""A custom upload's own steps (custom_upload.py): its file made the VOD's MP4, and the pause for review."""

from __future__ import annotations

from pathlib import Path

from archive_common.timeutil import format_hhmmss
from vex_platform.actor import Actor

from .. import ffmpeg
from ..context import JobContext, StepError, StepRefused


async def upload_source(ctx: JobContext) -> None:
    """The uploaded file (``payload.upload_file``) remuxed to the VOD's MP4 (``<id>.mp4``, which ``censor``,
    ``split`` and the preview read), then removed. Safe to re-run: once the MP4 is there it is kept."""
    out = ctx.default_mp4
    given = ctx.payload.get("upload_file")
    if out.exists() and ctx.payload.get("duration") and not (given and Path(given).exists()):
        return
    if not given or not Path(given).exists():
        raise StepRefused(f"the uploaded file {given or '(none)'} is gone; upload the video again")
    src = Path(given)
    ctx.log.info("remuxing %s -> %s", src.name, out.name)
    try:
        await ffmpeg.remux(src, out)
    except ffmpeg.FfmpegError as exc:
        raise StepRefused(
            f"the video's streams can't go into an MP4 as they are ({exc}); convert it to H.264/AAC and upload it again"
        ) from exc
    seconds = await ffmpeg.probe_duration(out)
    ctx.payload["duration"] = seconds
    await ctx.update_vod(duration=format_hhmmss(seconds))
    src.unlink(missing_ok=True)
    ctx.payload.pop("upload_file", None)
    await ctx.save()
    ctx.log.info("custom upload ready: %s (%s)", out.name, format_hhmmss(seconds))


async def upload_review(ctx: JobContext) -> None:
    """Pause the job once, before ``censor``: set censor ranges and check them in the preview, then resume it
    (Manage's Upload button) to censor, split and upload."""
    if ctx.payload.get("upload_review"):
        return  # resumed (or re-run): go on
    ctx.payload["upload_review"] = True
    if ctx.run is None:
        raise StepError("a custom upload pauses for review; only a runtime job can")
    ctx.log.info("waiting for review: set censor ranges if needed, then resume the job to upload")
    # Running, so it pauses at the next step boundary: before censor.
    await ctx.run.runtime.pause(ctx.job_id, actor=Actor("job", str(ctx.job_id), ctx.kind, "job"))
