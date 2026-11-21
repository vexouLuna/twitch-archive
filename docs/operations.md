# Operations and troubleshooting

[← README](../README.md)

| Task | Command |
|---|---|
| Logs | `docker compose logs -f worker` / `docker compose logs -f api` |
| Status | `docker compose ps` (both services have healthchecks, on `/healthz`). Is it serving? `curl -s localhost:3030/readyz` and `localhost:3031/readyz`: 503 names the dependency that failed |
| Failed jobs | `curl -s "${H[@]}" "$A/admin/jobs?state=failed"`, then `.../retry` |
| Queue a job without the API | `docker compose exec worker archive-worker enqueue chapters 2375792832` |
| Disk usage | `du -sh $ARCHIVE_HOST_DATA_DIR/*`; files are deleted after a successful upload unless `KEEP_*` is set. Failed jobs keep their files. |
| Stale work dirs | `$ARCHIVE_HOST_DATA_DIR/vods/<id>` with no job still active can be deleted by hand |

**Downloads fail with `PersistedQueryNotFound` or `No VOD playback token`.** Twitch rotated a GQL hash. Open twitch.tv in a browser, then DevTools → Network → filter `gql`. Play any VOD, find the `PlaybackAccessToken` request, and copy `extensions.persistedQuery.sha256Hash` into `ARCHIVE_GQL_HASH_PLAYBACK_TOKEN`. Do the same for `VideoCommentsByOffsetOrCursor` (open chat on a VOD), `VideoPreviewCard__VideoMoments` (the chapter list) and `NielsenContentMetadata`. Upstream ([TimIsOverpowered/archive](https://github.com/TimIsOverpowered/archive), `src/services/twitch/`) usually has the new values quickly. Then `docker compose up -d worker` and retry the failed jobs.

**Downloads fail with HTTP 403 on every variant.** Usher parameters changed or the VOD is sub-only. Compare `archive_worker/hls.py:vod_master_url` with upstream's `hls-utils.ts`.

**Chat crawl fails on `integrity` errors.** Twitch has started requiring client-integrity for the web client ID. Try another client ID for comments via `ARCHIVE_GQL_BACKUP_CLIENT_ID`.

**YouTube `invalid_grant`.** The refresh token is dead: it was revoked, unused for six months, or the OAuth app is in "Testing" mode (7-day tokens). The keep-alive task logs this within a day, and `/admin/youtube/status` shows it on demand. Run the consent flow again ([YouTube OAuth setup](youtube-oauth.md), Option B), then retry the failed upload jobs; they resume with the parts not yet uploaded. See "Keeping the token alive" in [YouTube OAuth setup](youtube-oauth.md).

**YouTube `uploadLimitExceeded` / `quotaExceeded`.** The job isn't retried; it is rescheduled: it stays `queued`, with `notBefore` set to when the limit should lift and the reason in `lastError`. The API quota (`quotaExceeded`) starts again at midnight Pacific, so it waits until then. Google doesn't say when a channel's upload limit (`uploadLimitExceeded`) lifts, so it waits a day. It resumes with the parts not yet uploaded. After 3 reschedules in a row with nothing uploaded in between, it fails; a manual retry then gets 3 more. The dashboard's quota count leaves out calls YouTube refused this way.

**Re-uploading a part that was deleted from YouTube.** A re-run (retry) skips the parts its payload lists under `uploaded`, but first asks YouTube whether each is still on the connected channel, and uploads again only the ones deleted from it; the new id replaces the old one. YouTube shows a channel its own uploads, private ones included, so a video it doesn't return was deleted only if it was uploaded to the channel connected now: each upload records its channel in the payload (`uploaded.<part>.channel`, taken from YouTube's answer to the upload). A part uploaded to another channel (the account was connected to a different one since) is skipped and the job log says so. A part uploaded before uploads recorded their channel counts as the connected channel's only if the stored token was connected through the OAuth flow (`connectedAt`) before the job began, since only a new connect changes the account; otherwise (connected later, or a token imported from the legacy config) it is skipped and logged. Cost: one `videos.list` (1 quota unit) per 50 parts, plus one `channels.list` (1 unit) when one is missing. When YouTube can't answer (an error, the daily limit), every part counts as still there and the log says so: a re-run never uploads a second copy. To force a re-upload anyway, remove the part from `uploaded` with the job page's **Edit payload**, then retry. A retry of a finished job starts at its first step, so its files must still be there: a custom upload whose files a `cleanup` job already removed stops at `upload_source` ("the uploaded file … is gone") and uploads nothing.

**The API returns stale data after a worker update.** Responses are cached for `ARCHIVE_CACHE_TTL_SECONDS` (5 min). Chat cursor pages are cached for 24h, because finished VODs don't change.
