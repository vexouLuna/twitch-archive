"""The job kinds: each is the list of step names (steps.STEPS) a run of it goes through, in order.

A leaf module, so a step can read them without importing jobs (which imports the steps).
"""

from __future__ import annotations

KINDS: dict[str, list[str]] = {
    # Stream went live (or /admin/hls/download): follow the VOD playlist, then process.
    "archive": [
        "capture",
        "finalize",
        "chapters",
        "chat",
        "emotes",
        "censor_check",
        "censor",
        "split",
        "upload",
        "describe",
        "previews",
        "cleanup",
    ],
    # /admin/download: full VOD (or a given file), split + upload, optional part range.
    "download": [
        "ensure_source",
        "fetch_vod",
        "finalize",
        "chapters",
        "censor_check",
        "censor",
        "split",
        "upload",
        "describe",
        "previews",
        "cleanup",
    ],
    "reupload": [
        "ensure_source",
        "fetch_vod",
        "finalize",
        "censor_check",
        "censor",
        "split",
        "upload",
        "describe",
        "previews",
        "cleanup",
    ],
    # Recording of the live stream itself (unmuted), uploaded as type "live". It saves the VOD's
    # chat and emotes too: without multi_track no archive job runs for the stream.
    "live": [
        "live_record",
        "resolve_vod",
        "finalize",
        "chapters",
        "chat",
        "emotes",
        "censor_check",
        "censor",
        "split",
        "upload",
        "describe",
        "previews",
        "cleanup",
    ],
    # /v2/live callback from an external recorder.
    "live_file": ["ensure_source", "chapters", "censor_check", "censor", "split", "upload", "describe", "previews"],
    "dmca": [
        "ensure_source",
        "fetch_vod",
        "finalize",
        "dmca_edit",
        "censor_check",
        "censor",
        "split",
        "upload",
        "describe",
        "previews",
        "cleanup",
    ],
    "part_dmca": [
        "ensure_source",
        "fetch_vod",
        "finalize",
        "censor_check",
        "censor",
        "split",
        "dmca_edit",
        "upload",
        "describe",
        "previews",
        "cleanup",
    ],
    # A synthetic VOD as one file, uploaded as type "compiled" with compile_youtube_upload on (steps/compile.py).
    "compile": ["compile_sources", "compile", "split", "upload", "compiled", "describe", "cleanup"],
    "chat": ["chat"],
    "logs_manual": ["logs_manual"],
    "chapters": ["chapters"],
    "emotes": ["emotes"],
    # One-off: give every emotes row saved before global sets were captured the current ones.
    "global_emotes_backfill": ["global_emotes_backfill"],
    # One-off: add 7TV's zero-width flags to channel sets saved before flags were kept.
    "seventv_flags_backfill": ["seventv_flags_backfill"],
    # One-off: the ad breaks (gaps) of live uploads made before they were saved, from their live jobs.
    "live_gaps_backfill": ["live_gaps_backfill"],
    "describe": ["describe"],
    # A pipeline's cleanup, queued by its own cleanup step to run keep_files_days later.
    "cleanup": ["cleanup"],
    # Chat from doomtp-bot into bot_logs, beside the archive job of the same VOD.
    "bot_chat": ["bot_chat"],
    # Queues a bot_chat run per VOD without bot chat (or per given VOD), newest first; they run one at a time.
    "bot_chat_backfill": ["bot_chat_backfill"],
    # Seek-bar previews for a VOD's uploads that have none, from a small download of each (steps/previews.py).
    "previews_fetch": ["previews_fetch"],
    # Queues a previews_fetch run per VOD with uploads without previews, newest first; they run one at a time.
    "previews_backfill": ["previews_backfill"],
}
