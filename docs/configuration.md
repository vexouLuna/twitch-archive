# Configuration

[← README](../README.md)

All settings are environment variables with the `ARCHIVE_` prefix. They can also be put in a `.env` file in the working directory. Lists are JSON, e.g. `ARCHIVE_RESTRICTED_GAMES='["Artifact"]'`. The source of truth is `packages/common/archive_common/config.py`, which groups them by concern (`CoreSettings`, `TwitchSettings`, `YouTubeSettings`, …); archive-api reads only `ApiSettings`, the shared groups and its own. Where a value comes from, first match wins: a dashboard override (worker only, the † settings), the environment, `.env`, its secret file, the default.

Settings marked **†** in the archive-worker table can also be changed from the admin dashboard while the worker runs ([Runtime settings](admin-api.md#runtime-settings)); the env value is then the default.

Secrets can also be files: one per setting, named like its variable in lowercase (`archive_admin_api_key`), in `/run/secrets` or the directory `ARCHIVE_SECRETS_DIR` names. A variable in the environment wins over its file.

In production, non-secret settings go in `.env` (see `.env.example`). Secrets are files in `secrets/` (see `secrets.example/` and its README), which `compose.yaml` mounts as docker secrets, so each container only receives the secrets it needs.

## Shared

| Variable | Default | Notes |
|---|---|---|
| `ARCHIVE_DATABASE_URL` | `postgresql+asyncpg://postgres:dev@127.0.0.1:55433/archive` | Use the `archive_api` or `archive_worker` role in production |
| `ARCHIVE_MIGRATION_DATABASE_URL` | – | Superuser URL used only by `alembic` (falls back to `ARCHIVE_DATABASE_URL`). Compose's `migrate` step builds it from `secrets/postgres_password` |
| `ARCHIVE_CHANNEL` | – | Channel name used in YouTube titles |
| `ARCHIVE_DOMAIN_NAME` | – | Frontend host, used in the "Chat Replay" link in descriptions |
| `ARCHIVE_TIMEZONE` | `UTC` | The date in YouTube titles |
| `ARCHIVE_LOG_LEVEL` | `INFO` | |
| `ARCHIVE_LOG_FORMAT` | `console` | `json` for one JSON object per line (containers) |
| `ARCHIVE_TWITCH_ID` / `ARCHIVE_TWITCH_USERNAME` | – | The channel to archive (numeric user ID and login) |
| `ARCHIVE_TWITCH_CLIENT_ID` / `ARCHIVE_TWITCH_CLIENT_SECRET` | – | Twitch app, used for Helix (app token). The API needs it for `/v2/badges`; the worker needs it for the monitor |

## Twitch GQL (update these when Twitch rotates them)

| Variable | Default |
|---|---|
| `ARCHIVE_GQL_CLIENT_ID` | `kimne78kx3ncx6brgo4mv6wki5h1ko` (twitch.tv web client) |
| `ARCHIVE_GQL_BACKUP_CLIENT_ID` | `kd1unb4b3q4t58fwlpcbzcbnm76a8fp` (used for comment cursors and moments) |
| `ARCHIVE_TWITCH_VIEWER_CLIENT_ID` | `ue6666qo983tsx6so1t0vnawi233wa` (Twitch's TV app: the device flow that [links an ad-free account](live-recordings.md#recording-without-ads-a-linked-twitch-account)) |
| `ARCHIVE_GQL_HASH_PLAYBACK_TOKEN` | `ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9` |
| `ARCHIVE_GQL_HASH_COMMENTS` | `b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a` |
| `ARCHIVE_GQL_HASH_MOMENTS` | `7399051b2d46f528d5f0eedf8b0db8d485bb1bb4c0a2c6707be6f1290cdcb31a` |
| `ARCHIVE_GQL_HASH_NIELSEN` | `2dbf505ee929438369e68e72319d1106bb3c142e295332fac157c90638968586` |

See [Troubleshooting](operations.md) for how to find new values.

## archive-api

| Variable | Default | Notes |
|---|---|---|
| `ARCHIVE_API_HOST` / `ARCHIVE_API_PORT` | `0.0.0.0` / `3030` | |
| `ARCHIVE_PAGINATE_DEFAULT` / `ARCHIVE_PAGINATE_MAX` | `10` / `50` | Same as Feathers |
| `ARCHIVE_RATE_LIMIT_POINTS` / `ARCHIVE_RATE_LIMIT_WINDOW_SECONDS` | `20` / `5` | Per client IP (`cf-connecting-ip` → `x-real-ip` → peer) |
| `ARCHIVE_API_TRUSTED_PROXIES` | `["*"]` | Peers (JSON list of addresses or CIDR ranges) whose `cf-connecting-ip`, `x-real-ip` and `X-Forwarded-For` are believed for the rate limit; any other peer counts as itself. The default believes every peer, as before: set it to your proxy (e.g. Docker's gateway for a proxy on this host) so a client that reaches the api directly can't pick its own bucket, or someone else's |
| `ARCHIVE_CACHE_TTL_SECONDS` | `300` | Response cache for `/vods`, `/emotes`, … |

## archive-worker

| Variable | Default | Notes |
|---|---|---|
| `ARCHIVE_DATA_DIR` | `/data` | VODs go in `vods/<vodId>/`, live recordings in `live/<streamId>/` |
| `ARCHIVE_VOD_DOWNLOAD` † | `true` | Archive every stream's Twitch VOD |
| `ARCHIVE_CHAT_DOWNLOAD` † | `true` | Save the chat replay into `logs` |
| `ARCHIVE_DOOMTP_URL` | empty | doomtp-bot's base URL. Set: each stream, once it ends, gets a `bot_chat` job that saves the bot's chat log (redeems, sub/raid notices, replies, removals) into `bot_logs`. Empty: off |
| `ARCHIVE_DOOMTP_LOGIN` | `ARCHIVE_TWITCH_USERNAME` | The channel's login on the bot |
| `ARCHIVE_DOOMTP_API_KEY` | empty | A read-scope key for the bot's log. With it, moderation entries and removed messages are saved too (flagged `deleted_at`/`cleared_at`); without it, the public log |
| `ARCHIVE_LIVE_RECORD` † | `false` | Record the live stream itself; see [Live recordings and multiTrack](live-recordings.md) |
| `ARCHIVE_MULTI_TRACK` † | `false` | With `LIVE_RECORD`, also upload the VOD copy (unlisted). Ignored when `LIVE_RECORD` is off |
| `ARCHIVE_FORCE_UPLOAD_MANUAL` † | `false` | With `LIVE_RECORD` and without `MULTI_TRACK`, still upload the VOD copy of archives started from the dashboard (see "Which copies are uploaded") |
| `ARCHIVE_YOUTUBE_UPLOAD` † | `true` | |
| `ARCHIVE_COMPILE_YOUTUBE_UPLOAD` † | `false` | Upload compiled VODs to YouTube: lets a `compile` job (started from Manage, never on its own) upload |
| `ARCHIVE_YOUTUBE_PUBLIC` † | `false` | Public instead of unlisted, for the main copy: the live copy when `LIVE_RECORD` is on, the VOD copy otherwise ([Live recordings and multiTrack](live-recordings.md)) |
| `ARCHIVE_COMPILE_YOUTUBE_PUBLIC` † | `false` | Compiled uploads public instead of unlisted; `ARCHIVE_YOUTUBE_PUBLIC` doesn't apply to them |
| `ARCHIVE_YOUTUBE_DESCRIPTION` † | `VOD` | Last line of every description |
| `ARCHIVE_YOUTUBE_TITLE` † | `{channel} Twitch {label} - {date}` | Upload title: `{channel}`, `{label}` (`VOD` or `Live VOD`), `{date}` (local), `{title}` (the stream's); ` PART n` is added when there are several |
| `ARCHIVE_YOUTUBE_TITLE_COMPILED` † | `{title}` | A compiled upload's title, the same fields; `{title}` is the synthetic VOD's. Both are checked at startup |
| `ARCHIVE_YOUTUBE_KEEPALIVE_HOURS` † | `24` | How often the worker refreshes the YouTube token; see [YouTube OAuth setup](youtube-oauth.md) |
| `ARCHIVE_RESTRICTED_GAMES` † | `[]` | Chapters of these games are left out of uploads ("No category": chapters without one, including the stretches Twitch's chapters leave out, which the chapters step fills with one) |
| `ARCHIVE_CENSOR_DESCRIPTORS` † | `["Sexual Content","Nudity"]` | A game whose IGDB content descriptors or themes contain one of these (case-insensitive) pauses the job for a censor review before upload; `[]` turns the check off. See "Censor check" below |
| `ARCHIVE_CENSOR_ALLOW_GAMES` † | `[]` | Games uploaded without the IGDB check (`RESTRICTED_GAMES` still wins: those are cut) |
| `ARCHIVE_MANUAL_STEPS` † | `{}` | Steps a job pauses before until you resume it, per job kind, e.g. `{"archive":["upload"]}`; see [Manual steps](admin-api.md#manual-steps) |
| `ARCHIVE_SPLIT_DURATION` † | `10800` | Maximum YouTube part length in seconds |
| `ARCHIVE_KEEP_HLS` / `ARCHIVE_KEEP_MP4` † | `false` | Keep files after upload |
| `ARCHIVE_KEEP_FILES_DAYS` † | `0` | Days to keep a job's files after upload: its cleanup step queues a `cleanup` job for then (0: clean up at once) |
| `ARCHIVE_PREVIEWS` † | `true` | Make seek-bar previews of each uploaded part; see [Seek-bar previews](admin-api.md#seek-bar-previews) |
| `ARCHIVE_PREVIEWS_FETCH_PAUSE_SECONDS` † | `60` | Pause between two YouTube downloads of the previews backfill |
| `ARCHIVE_PREVIEWS_YTDLP_ARGS` | `[]` | Extra yt-dlp arguments for those downloads, e.g. `["--cookies","/secrets/yt.txt"]` |
| `ARCHIVE_DRY_RUN` | `false` | Same as `run --dry-run` |
| `ARCHIVE_RUNNER_CONCURRENCY` † | `3` | Jobs run at once. Whatever this is, the heavy ffmpeg operations (remux, cut, mute, blackout or blur, preview sheets) run one of a kind at a time: a job that needs one another job is running waits for it |
| `ARCHIVE_MAX_ATTEMPTS` † | `3` | Tries of a failing step before its job is marked `failed` |
| `ARCHIVE_MONITOR_INTERVAL_SECONDS` † | `30` | How often Helix is checked for a live stream |
| `ARCHIVE_HLS_POLL_INTERVAL_SECONDS` | `60` | VOD playlist polling while the stream is live |
| `ARCHIVE_HLS_NO_CHANGE_THRESHOLD` | `10` | Unchanged polls before the capture is considered complete; also the polls of an ended VOD with segments that still fail before the capture step fails |
| `ARCHIVE_LIVE_POLL_INTERVAL_SECONDS` | `2` | Live playlist polling |
| `ARCHIVE_LIVE_END_THRESHOLD` | `90` | Polls with no new segments before the recorder checks whether the stream ended |
| `ARCHIVE_SEGMENT_CONCURRENCY` | `4` | Parallel segment downloads |
| `ARCHIVE_ADMIN_HOST` / `ARCHIVE_ADMIN_PORT` | `0.0.0.0` / `3031` | |
| `ARCHIVE_ADMIN_APART` | `false` | The admin API runs as its own process (`archive-worker admin`); the worker then serves only its health on the admin port. See [Running the admin API apart](deployment.md#running-the-admin-api-apart) |
| `ARCHIVE_ADMIN_API_KEY` | – | **Required** for any admin call from a script |
| `ARCHIVE_ADMIN_PASSWORD` | – | Password for the web dashboard's login; unset turns password login off. See [Browser access](admin-api.md#browser-access-dashboard) |
| `ARCHIVE_ADMIN_TRUSTED_PROXIES` | `[]` | Reverse proxies (addresses or CIDR ranges, JSON list) whose `X-Forwarded-For` / `X-Real-IP` is believed when rate-limiting logins and checking `ARCHIVE_ADMIN_PASSWORD_NETWORKS`, and whose `X-Forwarded-Proto` decides the password login cookie's `Secure`. A request forwarded by any other address gets no password login |
| `ARCHIVE_ADMIN_PASSWORD_NETWORKS` | this host and the private ranges | Where the password is accepted from (JSON list of addresses or CIDR ranges; `["*"]` = anywhere). Elsewhere it gets `403` |
| `ARCHIVE_ADMIN_TWITCH_IDS` | `[]` | Twitch user ids (JSON list) allowed in through Twitch sign-in |
| `ARCHIVE_ADMIN_AUTH_URL` | – | [vex-auth](https://github.com/vexouLuna/vex-auth) as browsers reach it. Twitch sign-in is on when this, the client secret, the redirect URL and at least one Twitch id are set |
| `ARCHIVE_ADMIN_AUTH_INTERNAL_URL` | `ARCHIVE_ADMIN_AUTH_URL` | vex-auth as the worker reaches it |
| `ARCHIVE_ADMIN_AUTH_CLIENT_ID` / `ARCHIVE_ADMIN_AUTH_CLIENT_SECRET` | `vods-admin` / – | This worker's client registration in vex-auth |
| `ARCHIVE_ADMIN_AUTH_REDIRECT_URL` | – | `/admin/signin/callback` as browsers reach it (through the dashboard's proxy); registered with vex-auth |
| `ARCHIVE_ADMIN_AUTH_CODE_REDIRECT_URL` | `ARCHIVE_ADMIN_AUTH_REDIRECT_URL` | The client's **first** redirect URI registered with vex-auth, which a code from its `POST /v2/codes` carries. Set it only if that isn't the callback above. See [Sign-in from a site's page](admin-api.md#sign-in-from-a-sites-page-post-adminsession-code) |
| `ARCHIVE_ADMIN_SITE_ORIGINS` | `[]` | Site origins (JSON list, e.g. `["https://vods.example.net"]`) whose pages may call `/admin/session` with cookies. CORS answers only them, and `POST /admin/session {code}` refuses any other `Origin`. The origin of `ARCHIVE_ADMIN_AUTH_REDIRECT_URL` (the site this worker serves through its proxy) is always included |
| `ARCHIVE_API_INTERNAL_URL` | `http://127.0.0.1:<api port>`; `http://api:<api port>` in `compose.yaml` | Where `/admin/health` checks archive-api |
| `ARCHIVE_MERGE_CANDIDATE_MINUTES` | `30` | `merge-candidates` lists VODs that started up to this long after a VOD ended; see [Merging and splitting VODs](admin-api.md#merging-and-splitting-vods) |
| `ARCHIVE_GOOGLE_CLIENT_ID` / `ARCHIVE_GOOGLE_CLIENT_SECRET` | – | Google OAuth client for YouTube |
| `ARCHIVE_GOOGLE_REDIRECT_URL` | `http://localhost:3031/admin/refreshtoken` | Must match a redirect URI on the Google client |
