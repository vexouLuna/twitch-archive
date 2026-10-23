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

**The API returns stale data after a worker update.** Responses are cached for `ARCHIVE_CACHE_TTL_SECONDS` (5 min). Chat cursor pages are cached for 24h, because finished VODs don't change.

## Converting merges to synthetic VODs

ADR 0004: the row-moving merges (`/admin/vods/{id}/merge`) become synthetic VODs. The command turns each
active merge of B into A into a synthetic VOD `A+B` that supersedes both, so their pages redirect into it.
It undoes the merge first, so A and B are again the VODs Twitch has.

```bash
docker compose exec worker archive-worker convert-splices          # dry run: what it would do; changes nothing
docker compose exec worker archive-worker convert-splices --apply  # do it
```

Each merge is one transaction. It carries over what was added to their uploads since (seek-bar `preview`,
live `gaps`) and moves hand-made synthetic VODs that play A onto A and B. Synthetic VODs that follow a
game (`auto`) update themselves on the monitor's next round. A merge is left alone, with the reason
printed, when it has an active job, censor ranges, a hidden VOD or other edits since the merge, and so
are splits and chains of splices. Fix those by hand and run it again. It is audited as `splice.convert`
and `synthetic.create`.

Run the dry run on a restored copy first (docs/development.md). After `--apply`, open each converted
VOD on the site: it should play the same, chat should line up, and the old `/vods/A` and `/vods/B` links
(with `?t=`) should land on the same moment.

