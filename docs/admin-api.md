# Admin API

[← README](../README.md)

The admin API is served by the worker on port 3031. Keep the port itself on your **private network**: never publish it to the internet (`ARCHIVE_ADMIN_PUBLISH_ADDR`, [Deployment (Docker Compose)](deployment.md)). The only public path to it should be the site's TLS proxy, for the routes a browser has to reach (`/admin/session`, `/admin/signin*` and the YouTube callback; vods.vexoul.net serves it at `/backend-admin`, [YouTube OAuth setup](youtube-oauth.md)). List that proxy in `ARCHIVE_ADMIN_TRUSTED_PROXIES`, or the password is refused through it (see [Browser access](#browser-access-dashboard)). Every call needs `Authorization: Bearer <ARCHIVE_ADMIN_API_KEY>`, or a dashboard login (see [Browser access](#browser-access-dashboard)). The old app accepted any word before the key, and this release still does, but logs a warning (once per word) when a script sends anything other than `Bearer`: the next release accepts only `Bearer` (ADR 0003).

Two routes need no key: `GET /healthz` (`{"status": "ok", "runningJobs": N}` while the process answers; the container's `HEALTHCHECK`, no dependency checks) and `GET /readyz` (the same once the database responds too, else 503 with `"status": "unavailable"` and `"error": "database: <type>"`). Point uptime monitors at `/readyz`. The dashboard's status bar reports the api as up when the api's `/readyz` answers 200.

Long-running actions enqueue a job and return right away with `{"error": false, "msg": "...", "jobId": N}`. Errors return `{"error": true, "msg": "..."}`. Request bodies use the same field names as the old app. `platform` is accepted and ignored.

```bash
KEY=...           # ARCHIVE_ADMIN_API_KEY
A=http://your-server:3031
H=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")
```

## /api/v2

The same API is moving to `/api/v2`, shared with doomtp-bot (the conventions are vex-platform's `docs/conventions.md`). It takes the same key or dashboard login, but answers differently:

- JSON keys are snake_case and times are ISO 8601 in UTC (`2026-09-30T12:00:00Z`).
- Lists are `{items, next_cursor}`. Pass `cursor=<next_cursor>` for the next page; `limit` is at most 500.
- Errors are `application/problem+json`: `{type, title, status, code, detail, request_id}`, plus `errors` for a body that did not validate. `code` is stable (`job_not_found`, `vod_spliced` …). With no credentials it answers 401, and with wrong ones 403.
- Every response has an `X-Request-ID` (yours, if you send one). Audit rows keep it.
- A write that is refused or fails is audited too (`request.denied`, `request.failed`).
- OpenAPI is at `/api/v2/docs`, behind the same login.

```bash
curl -s "${H[@]}" "$A/api/v2/jobs?state=paused&subject=vod:123"   # runs; also kind=, cursor=, limit=
curl -s "${H[@]}" -X POST "$A/api/v2/jobs" -d '{"kind":"download","subject":"vod:123","payload":{"type":"vod"}}'
curl -s "${H[@]}" -X POST "$A/api/v2/jobs/42/resume" -d '{"once":true}'   # also pause, retry, cancel; PATCH for gates
curl -s "${H[@]}" "$A/api/v2/jobs/42/events?cursor="                # tail: pass next_cursor back
curl -s "${H[@]}" "$A/api/v2/job-kinds"
curl -s "${H[@]}" "$A/api/v2/audit?action=vod.&target=vod:123"     # every actor; a trailing . or : is a prefix
curl -s "${H[@]}" "$A/api/v2/settings"
curl -s "${H[@]}" -X PATCH "$A/api/v2/settings" -d '{"keep_hls":true}'   # DELETE /api/v2/settings/keep_hls resets it
curl -s "${H[@]}" "$A/api/v2/storage?refresh=true"                 # DELETE /api/v2/storage/vods/123 frees one
curl -s "${H[@]}" "$A/api/v2/vods?q=title&hidden=false"            # also cursor=, limit= (at most 200); rows say uploaded, active_jobs
curl -s "${H[@]}" -X PATCH "$A/api/v2/vods/123" -d '{"title":"…","thumbnail_url":null}'
curl -s "${H[@]}" "$A/api/v2/vods/123/games"                       # PUT {"games":[...]} replaces them; GET's items can be sent back
curl -s "${H[@]}" -X PUT "$A/api/v2/vods/123/chapters" -d '{"chapters":[{"name":"…","gameId":"…","start":0,"length":600,"restricted":false}],"locked":true}'
curl -s "${H[@]}" -X PUT "$A/api/v2/vods/123/youtube" -d '{"youtube":[{"id":"…","type":"vod","part":1}]}'   # also /drive {"drive"}, /censor {"censor"}
```

`/api/v2/jobs` covers the job runtime's runs only. Jobs from the old `jobs` table (all finished) are still listed by `/admin/jobs`, with their logs, but nothing runs them any more and actions on them answer 409. A VOD's games, chapters, YouTube and Drive lists and censor ranges are in both, with the same rules (`/api/v2` answers `vod_merged`, `vod_synthetic` or `invalid_<list>`). Merging and splitting exist in both, done differently: see [Merging and splitting VODs](#merging-and-splitting-vods). The `/admin` routes stay until their clients have moved.

A v2 change writes its audit row in the same transaction as the change (`setting.update`, `setting.reset`, `vod.update`, `vod.games.replace`, `vod.chapters.replace`, `vod.youtube.replace`, `vod.drive.replace`, `vod.censor.replace`), with the fields before and after, so the two never disagree. A storage delete is files, so its row (`storage.delete`, with what it freed) comes right after.

## Jobs

```bash
curl -s "${H[@]}" "$A/admin/jobs"                       # latest 50 plus counts per state; filters below
curl -s "${H[@]}" "$A/admin/jobs?state=running,stopped" # states and/or groups, comma-separated
curl -s "${H[@]}" "$A/admin/jobs?kind=archive&vodId=...&limit=100"
curl -s "${H[@]}" "$A/admin/jobs/42"                    # one job: step, steps, attempts, lastError, payload
curl -s "${H[@]}" "$A/admin/kinds"                      # every job kind, its steps and its manual steps
curl -s "${H[@]}" -X POST "$A/admin/jobs" -d '{"kind":"download","vodId":"...","payload":{"type":"vod"}}'
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/pause"      # queued: now; running: when the current step ends
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/resume"     # run a paused job from its current step
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/resume" -d '{"once":true}'   # run one step, then pause again
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/retry"      # re-queue a failed job from the step it failed on
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/cancel"     # queued/paused: now; running: stops mid-step
```

Job states are `queued`, `running`, `paused`, `done`, `failed` and `cancelled`. The `state` filter also takes groups: `waiting` (`queued`, including jobs waiting out a retry backoff or rescheduled past a YouTube limit: their `notBefore` says until when), `stopped` (`paused`, `failed`, `cancelled`: they need you), `active` (`queued`, `running`, `paused`) and `finished` (`done`, `failed`, `cancelled`). A job whose step fails is retried after 2 minutes, then again after 4 minutes, and so on. On its third failure (`ARCHIVE_MAX_ATTEMPTS`) it is marked `failed` and keeps its files, so `retry` picks up from the same step. Cancelling a running job stops it immediately; the step's partial files stay on disk.

`POST /admin/jobs` starts any kind directly. Body fields: `kind` (required), `vodId`, `payload` (the same keys the specific routes put there, e.g. `type`, `stream_id`, `path`, `start_part`), `fromStep` (skip the steps before it), `pauseBefore` (this job's manual steps, see below) and `paused` (create it paused).

### Manual steps

A job can stop before chosen steps and wait for you. `ARCHIVE_MANUAL_STEPS` sets them per kind for every job, including the ones the monitor starts, e.g. `{"archive":["upload"],"live":["upload"]}` archives and splits automatically but leaves every upload for you to approve. A job's own `pauseBefore` (from `POST /admin/jobs`) replaces the setting for that job; `[]` means no manual steps. The worker refuses to start if the setting names an unknown kind or step.

When a job reaches a manual step it becomes `paused` at that step. Look at it (`GET /admin/jobs?state=paused`), then `resume` it, `resume` with `{"once":true}` to run just that step, or `cancel` it. Steps that already finished are never re-run, so resuming continues exactly where it stopped.

Job kinds and their steps:

| Kind | Steps | Started by |
|---|---|---|
| `archive` | capture → finalize → chapters → chat → emotes → censor_check → censor → split → upload → describe → previews → cleanup | monitor (stream went live), `/admin/hls/download` |
| `download` | ensure_source → fetch_vod → finalize → chapters → censor_check → censor → split → upload → describe → previews → cleanup | `/admin/download` |
| `reupload` | ensure_source → fetch_vod → finalize → censor_check → censor → split → upload → describe → previews → cleanup | `/admin/reupload` |
| `live` | live_record → resolve_vod → finalize → chapters → chat → emotes → censor_check → censor → split → upload → describe → previews → cleanup | monitor (when `LIVE_RECORD=true`) |
| `live_file` | ensure_source → chapters → censor_check → censor → split → upload → describe → previews | `/v2/live` |
| `dmca` | ensure_source → fetch_vod → finalize → dmca_edit → censor_check → censor → split → upload → describe → previews → cleanup | `/admin/dmca` |
| `part_dmca` | ensure_source → fetch_vod → finalize → censor_check → censor → split → dmca_edit → upload → describe → previews → cleanup | `/admin/part/dmca` |
| `chat`, `logs_manual`, `chapters`, `emotes`, `describe` | one step each | the matching admin routes |
| `cleanup` | one step | a pipeline's `cleanup` step, `ARCHIVE_KEEP_FILES_DAYS` later; a newer pipeline on the same folder cancels it, and it leaves files a run still needs (active, or failed since) |
| `global_emotes_backfill` | one step | `/admin/emotes/backfill` |
| `live_gaps_backfill` | one step | `/admin/live-gaps/backfill` |
| `bot_chat` | one step | monitor (stream ended, `DOOMTP_URL` set; separate from `archive`), `/admin/bot-chat` |
| `bot_chat_backfill` | one step: queues a `bot_chat` job per VOD | `/admin/bot-chat/backfill` |
| `previews_fetch` | one step | `/admin/previews` |
| `previews_backfill` | one step: queues a `previews_fetch` job per VOD | `/admin/previews/backfill` |
| `compile` | compile_sources → compile → split → upload → describe → cleanup | `POST /api/v2/jobs` with `{"kind": "compile", "subject": "vod:<synthetic id>", "payload": {"type": "compiled"}}` (the synthetic page's Compile button) |

When `split` finds nothing to upload (an empty VOD, or every chapter restricted), the job skips to `cleanup` and succeeds.

`ensure_source` uses `path` if one was given, otherwise the MP4 already on disk. With neither, `fetch_vod` downloads the whole VOD from Twitch again (only while Twitch still has it) and `finalize` converts it; both do nothing when there is a source already.

## Runtime settings

The settings marked † in [Configuration](configuration.md) can be changed without a restart. An override is kept in the database (`settings` table) and wins over the env value; resetting it goes back to the env value.

```bash
curl -s "${H[@]}" "$A/admin/settings"                   # each one: value, default (env), overridden, type, group, applies
curl -s "${H[@]}" -X PATCH "$A/admin/settings" -d '{"keep_hls":true,"runner_concurrency":2}'   # all or none
curl -s "${H[@]}" -X DELETE "$A/admin/settings/keep_hls"   # back to the env value
```

- Keys are the setting names without `ARCHIVE_`, in lower case. `GET` answers `{data: [{key, value, default, overridden, type, group, applies, help, min, max, updatedAt, updatedBy}]}`; `manual_steps` also carries `choices` (every kind's steps). `multi_track` and `force_upload_manual` carry `requires: "live_record"`: they do nothing while that is off, so the dashboard can disable it. `PATCH` and `DELETE` answer the same.
- **Types:** `bool`, `int` and `float` (within `min`–`max`), `text`, `list` (names) and `steps` (`{kind: [step, ...]}`, checked like `ARCHIVE_MANUAL_STEPS`). A `PATCH` with any refused value changes nothing (400).
- **`applies`:** `now` settings are read on every use: the monitor's next check (`vod_download`, `live_record`, `monitor_interval_seconds`), the runner's next pick (`runner_concurrency`, `max_attempts`), the next step boundary (`manual_steps`), the next token refresh (`youtube_keepalive_hours`) and the next dashboard load (`youtube_daily_quota`: the limit the dashboard counts the worker's YouTube API units against; Google shows no remaining quota, so the worker adds up what it spends itself, from midnight Pacific) and the YouTube check's next look (`youtube_check_hours`: how often every upload is checked on YouTube, 50 per `videos.list` unit: still there, not rejected or blocked, as long as uploaded, no part missing, and how much of the VOD the uploads hold; 0 leaves it to the dashboard's Check now; `GET /admin/youtube/videos` has the last result). `next job` settings are read when a job starts or resumes, so a running job finishes with what it started with.
- Both changes are audited with `{before, after}`. Secrets, keys, URLs, paths, the channel and the database are never editable here.

## Site tags

How [vods.vexoul.net](https://vods.vexoul.net) shows each VOD tag (its `/manage/tags` page edits them; archive-api serves them, [Public API reference](public-api.md)). They are kept in the database (`site_settings`, `site_tag_shapes`).

```bash
curl -s "${H[@]}" "$A/admin/site/tags"                  # {tags, updatedAt, updatedBy}; never saved: tags [] and nulls
curl -s "${H[@]}" -X PUT "$A/admin/site/tags" -d @tags.json   # {"tags": [...]}: the whole list, in order, all or none
curl -s "${H[@]}" -X PUT "$A/admin/site/tags/speedrun/shape" -H 'Content-Type: image/svg+xml' --data-binary @speedrun.svg
curl -s "${H[@]}" -X DELETE "$A/admin/site/tags/speedrun/shape"
```

- A tag is `{name, label, drawn, color, shape, width, height, rotate, text, textColor, textSize, textX, textY, textRotate, pattern, patternColor, patternSize, page, pageLabel, homeCard}`. `name`: `^[a-z0-9][a-z0-9-]{0,31}$`, unique. `label`: 1–40 characters. `drawn`: `true`/`false`. `width`/`height`: `null` or a whole number of px, 8–200. `rotate`: `null` (the site's default tilt) or a whole number of degrees, −180 to 180, turning the drawn tag clockwise.
- `color`, `textColor`, `patternColor`: `null` or a color of at most 160 characters, made only of letters, digits, spaces and `#.,%/()*+-`: a hex (`#` and 3–8 hex digits), a color name (3–20 letters), a theme token `var(--…)` (`--`, a letter, then `[a-z0-9-]*`, no fallback: any site's, `--vx-ok` or keeki's `--k-ok`), or one color function (`rgb rgba hsl hsla hwb lab lch oklab oklch color color-mix`) around the whole value. Inside it: theme tokens, color functions and `calc min max clamp`, at most 4 deep; no other function, `var()` or `--`. So `oklch(from var(--vx-accent) calc(l - 0.15) c h)` and `color-mix(in oklch, var(--vx-ok) 60%, white)` pass and `url()` doesn't. This is the sites' `isTagColor` (vods-core `src/app/lib/vodTags.ts`).
- Text drawn on the tag: `text` is `null` or 1–24 characters (trimmed). `textSize` is `null` or 6–48 px, `textX`/`textY` `null` or −100 to 100 px, and `textRotate` `null` or −180 to 180 degrees (clockwise), all whole numbers. With `text` `null`, all five of the others are stored as `null`.
- The pattern: `pattern` is `null`, `"stripes"` or `"checks"`, and `patternSize` is `null` or a whole number of px, 2–40. With `pattern` `null`, `patternColor` and `patternSize` are stored as `null`. A list saved before these fields existed reads them as `null`. `shape` is `v1/site/tags/{name}.svg?v=<hash>` or `null`; it is ignored in a `PUT` (the shape routes set it). Unknown fields are dropped.
- The type: `page` is `null` or a slug like `name` (not `vods`, `live`, `youtube`, `games`, `manage`, `admin` or `playthrough`, and no two tags the same): the tag's own menu entry and list page at `/{page}`. `pageLabel` is that page's title, 1–40 characters, required with a page and `null` without one. `homeCard`: `true`/`false`, whether the tag can feed a "latest" card on the homepage. Migration 0020 gives `compilation` the `playthroughs` page and a home card on a list saved before types.
- At most 32 tags, and `new`, `updated` and `compilation` (set automatically) must stay in the list. A refused list changes nothing: `400 {error, msg, tag, field}` names the first refused tag and field. A tag no longer in the list loses its shape.
- **A VOD's `tags`** (`PATCH /admin/vods/:id`, `PATCH /api/v2/vods/:id`, synthetic VODs) take the names in this list, less `new` and `updated` (the site works those out from dates). Until the list is saved, only `compilation`. A tag the list drops stays allowed on a VOD that already has it.
- **Shapes:** the raw SVG, at most 64 KB (`413`), as `Content-Type: image/svg+xml` (`415`), for a tag of the saved list (`404`). It is parsed with [defusedxml](https://pypi.org/project/defusedxml/) and rebuilt from an allow-list of shape elements and attributes. A `400` refuses `<script>`, `<foreignObject>`, `<iframe>`, `<image>` (embedded or linked rasters), `<a>`, animations, `on*` handlers, DOCTYPE and entities, an `href` that isn't `#id`, `url()` that isn't `url(#id)`, `@import` and `javascript:`. Editor metadata, other namespaces and comments are dropped. Colors stay as they are (`currentColor` too, in attributes, `style` and `<style>`): the site recolors only the `currentColor` parts, or the black ones in a file without any.
- Every route answers like `GET`. Changes are audited with `{before, after}` (the tag list, or the tag with its `shape` path; never the SVG) on target `site:tags` or `site-tag:<name>`.

## Site options

The site's own options: the homepage's cards (its `/manage/home` page edits them; archive-api serves them at `GET /v1/site/options`). Kept in `site_settings` (row `options`).

```bash
curl -s "${H[@]}" "$A/admin/site/options"                # {homeCards, playDefault, updatedAt, updatedBy}; never saved: the defaults and nulls
curl -s "${H[@]}" -X PUT "$A/admin/site/options" -d '{"homeCards": [{"kind": "latest"}, {"kind": "tag", "tag": "compilation"}, {"kind": "games"}]}'
```

- `homeCards`: the homepage's cards in order, at most 12. `{"kind": "latest"}` is the newest VOD (the stream while live), `{"kind": "tag", "tag": name}` the newest VODs with a tag whose type has `homeCard` (any name until the tag list is saved), `{"kind": "games"}` the most played games. One of each. The default is the three above.
- `playDefault`: what a synthetic VOD's watch page plays first: `"segments"` (the default; its sources' uploads, as composed) or `"compiled"` (its own uploads from a `compile` job, where it has them). The page offers the other too.
- A `PUT` takes any of the options; the others keep their value. A refused one changes nothing (`400 {error, msg}`). Audited as `site.options.update` with `{before, after}` on target `site:options`.

## Storage

Every job works in one folder of the data directory: `vods/<vodId>`, or `live/<streamId>` for a live recording. Files are normally deleted after upload (or `ARCHIVE_KEEP_FILES_DAYS` later); what is left is what `ARCHIVE_KEEP_HLS`/`KEEP_MP4` keep, jobs still in progress, and leftovers of failed jobs or deleted VODs.

```bash
curl -s "${H[@]}" "$A/admin/storage"                    # the disk, and each folder; ?refresh=true to scan again now
curl -s "${H[@]}" -X DELETE "$A/admin/storage/vods/123" # delete a folder's files
```

- `GET` answers `{disk: {total, used, free}, folders: [{area, name, path, bytes, files, modifiedAt, vod, jobs: {active, last}, stale}], cacheSeconds}`. `vod` is the VOD the folder belongs to (`{id, title, hidden}`, or `null`); `jobs` are the jobs working in it. Sizes are cached for 30 seconds (and dropped after a delete).
- A folder is **stale** when no job for it is queued, running or paused, and either no VOD belongs to it or its last job failed or was cancelled.
- `DELETE` answers `{path, bytes, files}` (what it frees, as the last scan measured it) and is audited with it. The folder moves into its area's `.trash/` before the answer and its files are removed in the background, since removing a VOD's files can outlast a proxy's timeout; the disk's free space catches up once they are gone, and whatever a restart interrupted is removed when the worker starts. It is refused (`409`) while a job for the folder is queued, running or paused, and only takes a folder name directly under `vods/` or `live/` (no symlinks). Paths are always relative to the data directory.

## Recipes

**Backfill a VOD the monitor missed** (the VOD must still be on Twitch):

```bash
curl -s "${H[@]}" -X POST "$A/admin/hls/download" -d '{"vodId":"2375792832"}'
```

This creates the `vods` row from Helix if needed, then runs the full `archive` pipeline.

**Only create the database row** (chapters and emotes are included; no video):

```bash
curl -s "${H[@]}" -X POST "$A/admin/generate/vod" -d '{"vodId":"2375792832"}'
```

**Download and upload again** (all parts, or a range):

```bash
curl -s "${H[@]}" -X POST "$A/admin/download" -d '{"vodId":"2375792832"}'
curl -s "${H[@]}" -X POST "$A/admin/download" -d '{"vodId":"2375792832","startPart":2,"endPart":3}'
curl -s "${H[@]}" -X POST "$A/admin/download" -d '{"vodId":"2375792832","path":"/data/manual/2375792832.mp4"}'
```

**Re-upload one part** (e.g. YouTube processing failed):

```bash
curl -s "${H[@]}" -X POST "$A/admin/reupload" -d '{"vodId":"2375792832","part":2,"type":"vod"}'
```

The new video replaces the part's entry in `vods.youtube`. The old YouTube video is not deleted; remove it in YouTube Studio.

**Handle a copyright claim (DMCA):**

1. In YouTube Studio → Content → the video → Copyright, open DevTools → Network and reload. Find the Studio API response that contains `receivedClaims` (search the responses for that word) and copy the array. This is the same data the old app took. Each claim has `type`, `claimPolicy.primaryPolicy.policyType` and `matchDetails.longestMatchStartTimeSeconds/DurationSeconds`.
2. For **one part**, send the claims from that part's video. The timestamps are relative to that part:
   ```bash
   curl -s "${H[@]}" -X POST "$A/admin/part/dmca" \
     -d '{"vodId":"2375792832","part":2,"type":"vod","receivedClaims":[ ... ]}'
   ```
3. For a VOD uploaded as a **single video** (or claims timed against the full VOD):
   ```bash
   curl -s "${H[@]}" -X POST "$A/admin/dmca" -d '{"vodId":"2375792832","type":"vod","receivedClaims":[ ... ]}'
   ```

Only blocking policies (`GLOBAL_BLOCK`, `MOSTLY_GLOBAL_BLOCK`, `BLOCK`) are acted on. Audio claims are muted, visual claims are blacked out, and audiovisual claims get both. The edited video is uploaded as a new part.

**Chat, chapters, emotes, descriptions:**

```bash
curl -s "${H[@]}" -X POST "$A/admin/logs" -d '{"vodId":"..."}'                 # crawl chat again (resumes, skips duplicates)
curl -s "${H[@]}" -X POST "$A/admin/logs/manual" -d '{"vodId":"...","path":"/data/manual/chat.json"}'  # {"comments":{"edges":[...]}}
curl -s "${H[@]}" -X POST "$A/admin/chapters" -d '{"vodId":"..."}'
curl -s "${H[@]}" -X POST "$A/admin/emotes" -d '{"vodId":"..."}'                # fill missing emote sets only
curl -s "${H[@]}" -X POST "$A/admin/emotes" -d '{"vodId":"...","force":true}'  # replace them with the current ones
curl -s "${H[@]}" -X POST "$A/admin/duration" -d '{"vodId":"..."}'             # set duration from Helix
curl -s "${H[@]}" -X POST "$A/admin/youtube/parts" -d '{"vodId":"...","type":"vod"}'  # rewrite descriptions
```

**Emote sets are history.** The `emotes` step saves the channel's FFZ, BTTV and 7TV sets plus the 7TV, BTTV and FFZ global sets (`global_emotes`) as they are when it runs. When the VOD already has a row, a re-run keeps what is saved and only fills sets that are empty; `force` overwrites everything with today's sets. Global sets filled in after the fact are marked `global_emotes_source: "backfilled"` instead of `"captured"`.

**Backfill global emotes** on rows saved before they were captured (run once; running it again changes nothing). It gives those rows today's global sets and never touches the channel sets. It fails without writing anything if a provider does not answer:

```bash
curl -s "${H[@]}" -X POST "$A/admin/emotes/backfill"
curl -s "${H[@]}" -X POST "$A/admin/emotes/backfill" -d '{"vodIds":["2375792832"]}'   # only these VODs
```

**Bot chat** comes from doomtp-bot's v2 log API (`/api/v2/channels/{login}/log`; entries are stored with epoch ms times, as before) into its own table, `bot_logs`, next to the replay chat in `logs`, which is never touched. The bot records the chat live, so like the replay chat it is read once, when the stream ends: the monitor then starts a `bot_chat` job with the VOD's final length from Twitch. Reading again (from the admin API) only adds rows or updates them (a message removed since). Gaps in the bot's coverage are logged as warnings on the job, and `vods.bot_chat` records the last read. Merged or split VODs are refused, like the other steps that fetch by VOD id. To read one VOD, or every VOD that has no bot chat yet (newest first, skipping merged or split ones and VODs with a `bot_chat` job already queued or running). The backfill queues one `bot_chat` job per VOD, each retrying on its own and shown under the backfill as related jobs; they share one lock, so they run one at a time beside the live jobs:

```bash
curl -s "${H[@]}" -X POST "$A/admin/bot-chat" -d '{"vodId":"2703890458"}'
curl -s "${H[@]}" -X POST "$A/admin/bot-chat/backfill"
curl -s "${H[@]}" -X POST "$A/admin/bot-chat/backfill" -d '{"vodIds":["2703890458"]}'   # only these VODs
```

**Ad breaks in live recordings.** A `live` recording leaves out the ads Twitch stitches into the stream, so the recording is shorter than the VOD and has holes in VOD time. The recording's `timeline` (in the job payload) says when each stretch was recorded. The `upload` step turns it into `gaps` on each live upload's `youtube` entry: `[[at, seconds], ...]`, where `at` is the time in that upload where VOD time skips ahead and `seconds` is how much VOD time is missing there (restricted chapters that overlap a gap are left out of it; they are cut anyway). A recording that ends before the VOD does gets a gap at the end of its last part. Whatever is missing before the first frame stays the upload's delay, as before. Gaps stay with their upload through re-uploads, admin edits of the same type, merges and splits. They apply only to the `live` uploads: the `vod` uploads have the whole VOD.

**Backfill gaps** onto the live uploads made before gaps were saved, from each finished `live` job's `timeline` (run once; running it again changes nothing). VODs whose live uploads already have `gaps`, merged or split VODs and recordings made before timelines were saved are skipped:

```bash
curl -s "${H[@]}" -X POST "$A/admin/live-gaps/backfill"
curl -s "${H[@]}" -X POST "$A/admin/live-gaps/backfill" -d '{"vodIds":["2895611509"]}'   # only these VODs
```

### Seek-bar previews

The sites show a frame of the video when you hover the timeline. The worker makes these as sprite sheets per YouTube upload, in that upload's own time, and keeps them in `<data dir>/previews/<youtube id>/<k>.jpg`. Each sheet has 10×10 tiles of 160×90. Frame `i` is the picture at `i × 10` s (or the keyframe just before), on sheet `i // 100`, column `i % 10`, row `(i // 10) % 10`. Once its sheets exist, the upload's `youtube` entry gets `preview: {v: 1, interval: 10, w: 160, h: 90, cols: 10, rows: 10, count}`, where `count` is the number of frames. An admin edit keeps it as long as the video id stays.

The `previews` step comes after `upload` in every kind that uploads, and works from the part files it just uploaded. Only keyframes are decoded, so a 3-hour 1080p part takes a minute or two. A part that fails is logged and skipped; previews never fail a job. A `--dry-run` writes the sheets to `<work dir>/previews/<part>` so you can look at them.

Uploads made before the step existed get their previews from `previews_fetch`. It downloads the upload's smallest video-only stream from YouTube with yt-dlp (240p or less, deno as its JavaScript runtime in the image), makes the sheets and deletes the download. The backfill queues one per VOD that has uploads without previews, newest first, skipping VODs that already have one queued. These jobs share one lock, so YouTube gets one download at a time, `ARCHIVE_PREVIEWS_FETCH_PAUSE_SECONDS` apart. A job fails, and is retried with the usual backoff, only when none of its uploads worked. If YouTube asks yt-dlp to sign in, give it cookies through `ARCHIVE_PREVIEWS_YTDLP_ARGS`.

```bash
curl -s "${H[@]}" -X POST "$A/admin/previews" -d '{"vodId":"2703890458"}'
curl -s "${H[@]}" -X POST "$A/admin/previews" -d '{"vodId":"2703890458","youtubeIds":["abcdefghijk"]}'   # only these
curl -s "${H[@]}" -X POST "$A/admin/previews/backfill"
curl -s "${H[@]}" -X POST "$A/admin/previews/backfill" -d '{"vodIds":["2703890458"]}'   # only these VODs
```

Sheets left behind by a replaced upload are not deleted. They are small, about 1–2 MB per hour of video.

`/admin/youtube/chapters` does the same as `/admin/youtube/parts`. Both rewrite every part's description from scratch: links to the other parts, the chat replay link, and the chapters inside that part.

**Create or delete rows by hand:**

```bash
curl -s "${H[@]}" -X POST "$A/admin/create" \
  -d '{"vodId":"123","title":"...","createdAt":"2026-01-01T20:00:00Z","duration":"03:00:00","platform":"twitch"}'
curl -s "${H[@]}" -X DELETE "$A/admin/delete" -d '{"vodId":"123"}'   # vod + logs + emotes + games
```

Paths in request bodies are **inside the worker container**, where `/data` is `ARCHIVE_HOST_DATA_DIR` on the host.

## Merging and splitting VODs

When a stream drops for technical reasons, Twitch makes two VODs of one broadcast: A, then B a few minutes later. Merging makes them one VOD on the site: a [synthetic VOD](#synthetic-vods) `A+B` that plays A, then B where it started, and supersedes both, so their pages redirect into it. A split makes `A-1` and `A-2`, at any second. Neither touches A or B, so undoing one is deleting it.

```bash
curl -s "${H[@]}" "$A/api/v2/vods/A/merge-candidates"                   # what to offer: {id, gap, titles_match, …}
curl -s "${H[@]}" -X POST "$A/api/v2/vods/A/merge" -d '{"source":"B"}'    # synthetic A+B; also "gap"
curl -s "${H[@]}" -X POST "$A/api/v2/vods/A/split" -d '{"at":3605}'       # synthetic A-1 and A-2, at any second
curl -s "${H[@]}" -X DELETE "$A/api/v2/synthetic/A+B"                     # undo: A and B list and play as before
```

Both are audited as `synthetic.create`, with `detail` naming the merge or split. `GET /admin/vods/{id}/merge-candidates` gives the same list in the `/admin` shape.

The older merge and split moved rows between the VODs. They are gone (ADR 0004): every one was converted into a synthetic VOD, and `POST /admin/vods/{id}/merge`, `unmerge`, `split` and `unsplit` answer `410` with the `/api/v2` route to use. A VOD merged away by them still has `merged_into` in the public API.

## Synthetic VODs

A synthetic VOD is made of windows of real ones (segments), each placed at `at` seconds on its own timeline: a merge, the halves of a split, or a playthrough of one game across streams. It reads its video, chapters and chat from those VODs; nothing is copied. With `supersedes`, the VODs it is made of leave the lists and redirect into it. Its id is never only digits, since those are Twitch's.

```bash
curl -s "${H[@]}" -X POST "$A/api/v2/synthetic" -d '{"id":"elden-ring","tags":["playthrough"],"segments":[{"vod_id":"1","start":5,"end":3600},{"vod_id":"2"}]}'
curl -s "${H[@]}" -X PUT "$A/api/v2/synthetic/elden-ring" -d '{"title":"Elden Ring"}'   # only the fields sent
curl -s "${H[@]}" "$A/api/v2/vods?synthetic=true"                                       # only synthetic VODs
curl -s "${H[@]}" "$A/api/v2/playthrough-candidates?game_id=512953"                     # every window of a game, oldest first
```

A segment's `start` defaults to 0, `end` to its VOD's end, and `at` to right after the segment before. Changes are audited as `synthetic.create`, `synthetic.update` and `synthetic.delete`.

A playthrough can keep itself up to date: send `"auto": {"game_id": "512953"}` (on create, instead of `segments`, or on a PUT) and its segments become every window of that game. Each monitor round writes them again when they differ, so a new stream of the game or a source's edited chapters reach it; that change is audited as `synthetic.update` by `system`. Sending `segments` without `auto` is a hand edit and stops it, as does `"auto": null`. The view says what it follows in `auto` (`null`: nothing).

A synthetic VOD can also be made into one file and uploaded as its own video: a `compile` job cuts each window from its source's kept MP4 (`ARCHIVE_KEEP_MP4`, at `<vod_dir>/<id>/<id>.mp4`), joins them, then splits, uploads and describes it like a VOD. Its uploads go on the synthetic VOD's row as `type: "compiled"` (unlisted unless `ARCHIVE_COMPILE_YOUTUBE_PUBLIC`), and only with `ARCHIVE_COMPILE_YOUTUBE_UPLOAD` on (off by default; `youtube_upload` must be on too); with it off the job runs but uploads nothing (the file stays only with `ARCHIVE_KEEP_MP4`). The segments stay what the VOD is: changing them later (by hand, or a playthrough following its game) doesn't change the compiled uploads, but marks them outdated (`compiled.outdated` in `GET /api/v2/synthetic/{id}`, from `synthetic.compiledFrom`); Manage then offers Recompile (`"replace": true` replaces the old ones). A gap in the timeline (a playthrough's time between runs) is left out of the file: the windows play back to back and each upload's `gaps` records what it skips, so the player maps the file onto the VOD's time as it does a live recording's ad breaks. Refused: a source without a kept MP4 and a source with censor ranges (its kept MP4 is uncensored). A source with DMCA claims (its done `dmca` / `part_dmca` jobs, listed by `GET /api/v2/synthetic/{id}/dmca` as `overlapping`, the ranges in a window it plays, and `other`; a live copy's claims count as overlapping) compiles only with `"accept_dmca": true`, which Manage sends after a warning listing them. Each window is re-encoded (H.264 + AAC) so it cuts on the exact frame, slower than a stream copy; each window's measured length is used.

## Browser access (dashboard)

A web dashboard logs in instead of carrying the API key: with Twitch through [vex-auth](https://github.com/vexouLuna/vex-auth) (the sign-in shared by the vexoul.net sites), or with `ARCHIVE_ADMIN_PASSWORD` from the local network. The key keeps working for scripts; every `/admin/*` route accepts any of them. The audit log records the actor as `api-key`, `password` or `twitch:<id>`.

| Route | |
|---|---|
| `GET /admin/session` | No auth. `{"authenticated", "csrf", "expiresAt", "passwordLogin", "twitchLogin", "user"}`. `passwordLogin`: the password is offered to this address. `user`: the Twitch user, `null` for a password login |
| `POST /admin/session` `{"password"}` | Logs in: the same shape plus the `archive_admin` cookie. `401` wrong password, `403` from outside `ARCHIVE_ADMIN_PASSWORD_NETWORKS`, `429` + `Retry-After` after 5 failed logins in 5 minutes from one address, `404` when password login is off |
| `GET /admin/signin?next=/admin/...` | Starts a Twitch sign-in: redirects to vex-auth, which comes back to `/admin/signin/callback`. That sets the same cookie and redirects to `next`, or to `/admin/login?auth_error=<denied\|expired\|twitch\|not_allowed\|unavailable\|misconfigured>&next=...`. `404` when Twitch sign-in is off |
| `POST /admin/session` `{"code"}` | A Twitch sign-in from a code the site's page got from vex-auth, with no redirect: see [Sign-in from a site's page](#sign-in-from-a-sites-page-post-adminsession-code) |
| `DELETE /admin/session` | Logs out (`204`) and clears the cookie |

Only `ARCHIVE_ADMIN_TWITCH_IDS` get in through Twitch. A Twitch session is checked with vex-auth again every minute, so signing out everywhere on any vexoul.net site ends it too; if vex-auth can't be reached, the session stands until it can.

The cookie is `HttpOnly; Secure; SameSite=Strict` and lasts 8 hours. Sessions are kept in the `admin_sessions` table (by the sha256 of the cookie's token, never the token itself), so they outlast a restart; expired ones are dropped at the next login. With the cookie, every request except `GET` must also send the session's `csrf` value as `X-CSRF-Token`, or it gets `403`. `Secure` means browsers only send the cookie over HTTPS (or to `localhost`), so serve the dashboard through something that terminates TLS. The one exception is a password login that reached the worker over plain HTTP, such as a LAN address with no TLS: that cookie goes without `Secure`, since a browser would otherwise drop it (the password only works from `ARCHIVE_ADMIN_PASSWORD_NETWORKS` anyway). Twitch sign-in always comes back over HTTPS. If a reverse proxy sits in front, list its address in `ARCHIVE_ADMIN_TRUSTED_PROXIES` so the login rate limit and the password networks see the real client and not the proxy, and so its `X-Forwarded-Proto` says whether the browser used HTTPS. Forwarded headers from any other address are ignored, and a request that carries them (`X-Forwarded-For`, `Forwarded` or `X-Real-IP`) from an address not in that list is refused the password with `403`: it came through a proxy nobody listed, and a proxy on this host (or Docker's port forward) would otherwise make every internet client look local. Twitch sign-in is unaffected.

### Sign-in from a site's page (`POST /admin/session {code}`)

A site page can learn, in two `fetch`es and with no page redirect, whether the person signed in to the
vexoul.net sites is an archive admin, and get the dashboard session if they are. It needs vex-auth
`v0.3.0` or later.

1. The page asks vex-auth for a one-time code for this worker's client:

   ```js
   const me = await fetch(`${AUTH}/v1/me`, { credentials: "include" });   // 401: not signed in, so not an admin
   const { csrf } = await me.json();
   const r = await fetch(`${AUTH}/v1/codes`, {
     method: "POST",
     credentials: "include",
     headers: { "Content-Type": "application/json", "X-Vex-CSRF": csrf },
     body: JSON.stringify({ client_id: "vods-admin" }),   // ARCHIVE_ADMIN_AUTH_CLIENT_ID
   });
   // 200 {code}; 401 signed_out (not an admin); 400 unknown_client, 403 csrf, 409 scope_missing, 429 rate_limited
   ```

2. It hands the code to the worker, as the dashboard reaches it:

   ```js
   const s = await fetch(`${WORKER}/admin/session`, {
     method: "POST",
     credentials: "include",
     headers: { "Content-Type": "application/json" },
     body: JSON.stringify({ code }),
   });
   ```

The worker redeems the code with vex-auth's `POST /v1/token` (its client id and secret, and the
redirect URI the code carries: `ARCHIVE_ADMIN_AUTH_CODE_REDIRECT_URL`), then:

| Answer | When |
|---|---|
| `200` `{"admin": true, "authenticated": true, "csrf", "expiresAt", "passwordLogin", "twitchLogin", "user"}` + the `archive_admin` cookie | The user is in `ARCHIVE_ADMIN_TWITCH_IDS`. The same session, cookie and once-a-minute check with vex-auth as `/admin/signin/callback` makes; send `csrf` as `X-CSRF-Token` on writes |
| `200` `{"admin": false}`, no cookie | Signed in, but not an admin |
| `400` `invalid_code` | The code is missing, unknown, expired (codes last 60 seconds), already used, or its vex-auth session has ended. A wrong `ARCHIVE_ADMIN_AUTH_CODE_REDIRECT_URL` looks like this too |
| `403` `origin_not_allowed` | The request's `Origin` is neither in `ARCHIVE_ADMIN_SITE_ORIGINS` nor `ARCHIVE_ADMIN_AUTH_REDIRECT_URL`'s. The code is not redeemed |
| `404` `signin_off` | Twitch sign-in isn't configured |
| `502` `misconfigured` | vex-auth refused the worker's client id or secret |
| `503` `unavailable` | vex-auth couldn't be reached or is rate-limiting; try again |

Refusals are RFC 9457 problem details (`application/problem+json`, with `code` as above). A request
without an `Origin` header (not a browser) is let through; a browser always sends one with a `POST`.
`/admin/session` answers CORS for `ARCHIVE_ADMIN_SITE_ORIGINS` only (credentials allowed; `GET`,
`POST`, `DELETE`; headers `Content-Type` and `X-CSRF-Token`), and no other admin route answers CORS at
all. The cookie stays `SameSite=Strict`, so the page must be same-site with the worker's address (a
sibling host of the same domain, or the site's own proxy to the worker). The audit log records the
sign-in as `session.login` by `twitch:<id>`, with detail `{"via": "code"}`; the code itself is never
stored. Any script on one of those origins can do this for whoever is signed in; it gains nothing it
couldn't already do there with that person's cookies.

What the dashboard reads and edits (all take the key or the cookie):

```bash
curl -s "${H[@]}" "$A/admin/health"                     # worker, api, youtube token, live stream, job counts + last 5 failures
curl -s "${H[@]}" "$A/admin/jobs?before=120&limit=50"   # older jobs: ids below 120
curl -s "${H[@]}" -X PATCH "$A/admin/jobs/42" -d '{"pauseBefore":["upload"],"pauseNext":false}'
curl -s "${H[@]}" "$A/admin/jobs/42/events?after=0&limit=200"   # log lines, step changes, progress; poll with after=<next>
curl -s "${H[@]}" "$A/admin/vods?q=title&hidden=true&limit=50"   # every VOD (hidden, merged too); page with before=<next>
curl -s "${H[@]}" "$A/admin/vods/123"                   # as GET /vods/123, plus hidden, chaptersLocked, botChat, censor, recent jobs
curl -s "${H[@]}" -X PATCH "$A/admin/vods/123" -d '{"title":"...","hidden":true}'
curl -s "${H[@]}" -X PATCH "$A/admin/vods/123" -d '{"thumbnailUrl":null,"duration":"05:12:00","createdAt":"2026-09-30T18:00:00Z"}'
curl -s "${H[@]}" -X PUT "$A/admin/vods/123/chapters" \
  -d '{"locked":true,"chapters":[{"name":"Just Chatting","gameId":"509658","imageTemplate":"https://.../509658-{width}x{height}.jpg","start":0,"length":3600,"restricted":false}]}'
curl -s "${H[@]}" -X PUT "$A/admin/vods/123/youtube" -d '{"youtube":[{"id":"abc","type":"vod","part":1}]}'
curl -s "${H[@]}" -X PUT "$A/admin/vods/123/drive" -d '{"drive":[{"id":"...","type":"live"}]}'
curl -s "${H[@]}" "$A/admin/vods/123/emotes"            # the saved emotes row, or null
curl -s "${H[@]}" "$A/admin/vods/123/games"             # the games rows, as GET /games renders them
curl -s "${H[@]}" -X PUT "$A/admin/vods/123/games" \
  -d '{"games":[{"start_time":0,"end_time":3600,"game_id":"512953","game_name":"Elden Ring","video_provider":"youtube","video_id":"abc"}]}'
curl -s "${H[@]}" "$A/admin/twitch/games?query=chat"    # Twitch categories: gameId, name, imageTemplate
curl -s "${H[@]}" "$A/admin/twitch/videos"              # the channel's VODs still on Twitch, with what the archive has of each
curl -s "${H[@]}" -X POST "$A/admin/twitch/import" -d '{"vodIds":["123","456"]}'   # archive them, paused before capture
curl -s "${H[@]}" "$A/admin/audit?before=&limit=50"     # who changed what
```

`/admin/health`'s `doomtp` is the connection to doomtp-bot for the dashboard: `{configured, keyed, login, ok, listening, since, error, checkedAt}`. It asks the bot's `/log/coverage` about the last five minutes, at most once a minute: `ok` is whether the bot answered (`null` without `ARCHIVE_DOOMTP_URL`), `listening` whether it is in the channel's chat right now and `since` since when (or when it left), and `error` what went wrong (`HTTP 403: the log isn't public and no key is set`, `ConnectError` …).

- **Health** refreshes the YouTube token at most every 10 minutes; `/admin/youtube/status` always does.
- **Job events** keep about the last 1000 entries per job. `capture`, `split` and `upload` add `progress` (`{"done", "total", "unit"}`, unit `parts`, `bytes` or `percent`).
- **Chapters** must be sorted, must not overlap, must have `length > 0` and must end inside the VOD. They are stored in the legacy shape, with `image` filled in at 40x53 for older readers. `"locked": true` makes the automatic `chapters` step skip the VOD. `POST /admin/chapters` with `{"force": true}` (or a job payload with `"force": true`) overrides the lock.
- **VODs on Twitch** (`GET /admin/twitch/videos`): every past broadcast Twitch still keeps for the channel (7 to 60 days, depending on the channel), newest first, as `{data}`, each `{id, title, createdAt, duration, thumbnailUrl, inArchive, uploaded, archiveJob}`. `inArchive` means the VOD has a row, `uploaded` means it has YouTube parts, and `archiveJob` is `{id, state, step}` for a queued, running or paused archive job, otherwise `null`. `POST /admin/twitch/import` with `{"vodIds": [...]}` queues an archive job for each one (creating its row from Twitch when it has none), paused before `capture`; send `"paused": false` to let them run. It answers `{added: [{vodId, jobId}], skipped: [{vodId, reason}]}`: a VOD no longer on Twitch, or with an archive job already queued, running or paused, is skipped.
- **YouTube entries** keep their stored thumbnail, and keep their duration unless you send a new one.
- **The VOD list** (`GET /admin/vods`) has every VOD, hidden and merged ones included, newest first: `{data, next}`, each row `{id, title, createdAt, duration, duration_seconds, thumbnail_url, stream_id, hidden, merged_into}`. `q` matches the id exactly or the title (case-insensitive substring), `hidden=true|false` filters, `limit` is at most 200, and `before=<next>` pages back.
- **VOD fields** (`PATCH /admin/vods/{id}`): any of `title`, `hidden`, `thumbnailUrl` (an http(s) URL, or `null` for the site's default), `duration` (`HH:MM:SS`; it must still hold the chapters and games rows) and `createdAt` (ISO with an offset), and `cutReason` (why the VOD was cut, up to 500 characters; `null` or blank clears it). The public API shows it as `cut_reason`, only while set; the site shows it on the page of a VOD cut in full. A merged VOD takes only `hidden`. **Hidden** takes the VOD off the public API as if it were missing (see [Public API reference](public-api.md)); the dashboard still has it, and unhiding brings it back.
- **Games rows** (`PUT /admin/vods/{id}/games`) replace the VOD's rows. Send them in the shape `GET` returns (`id`, `vodId` and the dates are ignored): `start_time` and `end_time` in seconds, sorted, not overlapping and inside the VOD, a `game_name`, and optionally `game_id`, `title`, `video_provider`, `video_id`, and `thumbnail_url` and `chapter_image` (http(s) URLs). Refused on a merged VOD.
- **Censor ranges** (`PUT /admin/vods/{id}/censor` with `{"censor": [...]}`) replace what is hidden in the VOD's uploads, for what Twitch allows and YouTube doesn't (in-game nudity, say). Each range is `{start, end, video, mute, note?}` in VOD seconds, inside the VOD: `video` is `"blur"`, `"black"` or `null`, `mute` silences the audio, and a range must do one or the other. Ranges may overlap (black wins over blur). The `censor` step applies them to the whole source before `split`, so every upload made from then on carries them; a live recording's ranges are moved onto the recording, and a range it didn't record is skipped. Only the covered clips are re-encoded. Saving ranges doesn't touch what is already on YouTube: re-archive for that. Refused on merged and synthetic VODs, and merging or splitting a VOD with ranges is refused until they are removed. To set them before a new VOD's first upload, add `censor` to its manual steps (`ARCHIVE_MANUAL_STEPS` or `pauseBefore`).
- **Censor check.** The `censor_check` step, just before `censor`, looks up the games of the chapters about to be uploaded on IGDB (Twitch's categories are IGDB games; the Twitch app's credentials work there too, results are cached a day). A game in `RESTRICTED_GAMES` is cut and not looked up, one in `CENSOR_ALLOW_GAMES` is uploaded as it is, and one whose age-rating content descriptors or themes contain one of `CENSOR_DESCRIPTORS` pauses the job before `censor`, with the games and what matched in the job payload's `censor_review`. An IGDB error pauses it the same way. Then either set censor ranges and resume, resume as it is, or `POST /admin/jobs/{id}/censor-review` with `{"decision": "upload"}` (resume) or `{"decision": "cut"}` (mark those games' chapters restricted, lock the chapters, resume). Without a Twitch app the check is skipped with a warning. A re-archive checks again: add a game to `CENSOR_ALLOW_GAMES` (or `RESTRICTED_GAMES`) to stop being asked.
- **Deleting uploads** (`POST /admin/vods/{id}/youtube/delete` with `{"videos": [ids]}`) deletes those of the VOD's uploads from YouTube (50 quota units each) and takes them off its list; one already gone from YouTube is just taken off. A describe job per copy that still has parts rewrites their descriptions. It answers `{deleted, failed, describeJobs, vod}`; one that YouTube refused stays on the list, in `failed`.
- **Re-archiving** (`POST /admin/vods/{id}/rearchive` with `{type, part?, path?}`) uploads the VOD again with today's settings and censor ranges: a `download` job, or `reupload` for one `part`, with `"replace": true`. The source is `path`, the local copy, or Twitch while it still has the VOD. Each new part is uploaded first, then the upload it replaces is deleted from YouTube (one that fails is logged and stays on YouTube, listed in the job payload's `not_removed`), so the VOD always has its uploads. Re-archiving the whole VOD into fewer parts also deletes the old parts left over. Refused on merged and synthetic VODs.
- **Edits show up on the public API at once:** the worker sends a Postgres `NOTIFY`, and archive-api drops its cached responses for that VOD and for the lists that include it.
- **Audit log:** every state-changing admin request that succeeds is stored as `{at, actor, actorLogin, action, target, detail}`. `actor` is `password`, `api-key` or `twitch:<id>`, `actorLogin` is the Twitch login behind a `twitch:<id>` (otherwise `null`), `action` names what was done (`vod.update`, `setting.reset`, `job.cancel` …; the routes' names are in `packages/common/archive_common/audit.py`), `target` is `vod:<id>`, `job:<id>`, `setting:<key>` or `storage:<area>/<name>` when there is one, and `detail` is the request body. A VOD edit (`PATCH /admin/vods/{id}`, `PUT …/games`) and a settings change store `{before, after}` instead: the fields or settings it changed, or the rows. A login password is never stored. Actions on jobs are recorded by the job runtime, whichever way they were made. The rows live in `audit_log`, which the monitor's and jobs' own actions go to as well; this route lists only the admins'. Entries from before it (the old `admin_audit` table) were copied in, with their actions renamed the same way.
