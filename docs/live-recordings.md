# Live recordings and multiTrack

[← README](../README.md)

**Why record the live stream?** Twitch mutes copyrighted music in finished VODs, in 30-minute blocks (the `-muted` segments). A recording taken while the stream is live has the original audio. The downside is that anything that needed muting is now in your YouTube upload and may be claimed; the [DMCA recipes](admin-api.md#recipes) handle that.

**How it works (`ARCHIVE_LIVE_RECORD=true`):**

1. The monitor sees the channel go live and queues a `live` job for that stream ID.
2. `live_record` gets a live playback token and follows the source-quality playlist every 2 seconds. It writes segments to `/data/live/<streamId>/hls/`.
3. Twitch ad segments (stitched-ad `DATERANGE`s and `Amazon` segments) are skipped and turned into discontinuities. If the connection drops, the recorder reconnects with a new token.
4. The recording ends when the playlist ends, or when no new segments have appeared for about 3 minutes and Helix confirms the stream is over.
5. `resolve_vod` attaches the recording to the stream's VOD row. After that the job runs finalize → chapters → split → upload as `type: "live"`, with the title `… Twitch Live VOD - YYYY-MM-DD`.
6. The recording is not on the VOD's timeline: it starts after the stream does, and ad breaks and reconnects leave holes in it. The recorder saves each segment's `EXT-X-PROGRAM-DATE-TIME` in `hls/segments.jsonl`, and the job keeps the stretches recorded without a gap in `payload.timeline` (`[recording start, wall-clock start, length]`). `split` and `describe` move the VOD's chapters onto the recording through it (VOD time is wall-clock time since the VOD's `createdAt`), so restricted games are cut, and the description's chapters placed, where they are in the recording. A chapter boundary that falls in a hole moves to the hole's end. Recordings made before timelines were saved, and `live` jobs without a recording (reupload, DMCA), use the VOD's chapters as they are.

**Which copies are uploaded:**

| `LIVE_RECORD` | `MULTI_TRACK` | Uploaded | `YOUTUBE_PUBLIC=true` makes public |
|---|---|---|---|
| false | – | VOD copy | the VOD copy |
| true | false | **live copy only**: no `archive` job runs; the `live` job saves the VOD's chapters, chat and emotes | the live copy |
| true | true | both | the live copy (the VOD copy stays unlisted) |

`FORCE_UPLOAD_MANUAL` (†, default off) changes the middle row for archives started from the dashboard (added from Twitch, `/admin/hls/download`, `/admin/download`, `/admin/reupload`, a re-archive, DMCA): their VOD copy is uploaded too, and is the main copy (public with `YOUTUBE_PUBLIC`) while the VOD has no live upload, as for a stream the live recording missed. The monitor's own jobs are unchanged.

The frontend (`YoutubeVod.js`) plays `live` entries whenever a VOD has any, and falls back to `vod` entries otherwise. Chat sync works the same way for both.

**Disk usage:** the source quality is about 6–8 Mbit/s, which is roughly 3–3.5 GB per hour per copy. With both copies you need twice that until cleanup runs.

**External recorders (`POST /v2/live`)** still work. A recorder saves an MP4 and calls:

```bash
curl -s "${H[@]}" -X POST "$A/v2/live" -d '{"streamId":"318194011992","path":"/data/live/318194011992/rec.mp4"}'
```

That queues a `live_file` job. As with the old app, the call returns **404 when `MULTI_TRACK` is off**, which tells the recorder to delete its file. `driveId` is stored in `vods.drive` but nothing is uploaded to Drive.

## Recording without ads (a linked Twitch account)

Twitch stitches ads into the live stream, and the recorder can only skip them: each ad break is time missing from the live copy (its upload's `gaps`). Twitch leaves the ads out for an account that is ad-free on the channel: one with **Turbo** (every channel), or a **sub** when the channel gives subs ad-free viewing. Link such an account and the worker asks for the live playback token as it; nothing else is done as that account (VOD downloads, chat and chapters stay anonymous).

**Link it from the Manage screen** (Overview → *Twitch (ad-free)* → *Link Twitch account*): the dashboard shows a code; open twitch.tv/activate signed in as the account (the broadcaster's own, typically) and enter it. That is Twitch's device flow, as `ARCHIVE_TWITCH_VIEWER_CLIENT_ID`. From the API:

```bash
curl -s "${H[@]}" -X POST "$A/admin/twitch/viewer/device"             # {flowId, userCode, verificationUri, expiresIn, interval}
curl -s "${H[@]}" "$A/admin/twitch/viewer/device/<flowId>"            # {state: pending | linked | failed, status?, error?}
curl -s "${H[@]}" "$A/admin/twitch/viewer"                            # {linked, valid, login, userId, clientId, method, connectedAt, expiresAt, error, checkedAt}
curl -s "${H[@]}" -X PUT "$A/admin/twitch/viewer" -d '{"token":"…"}'  # link by pasting a token instead
curl -s "${H[@]}" -X DELETE "$A/admin/twitch/viewer"                  # unlink
```

If recordings still have ads with the device-flow token, link by pasting instead: the `auth-token` cookie of twitch.tv signed in as the account (the *Paste a token* button does the same). The worker validates either token with Twitch and keeps the client id it was issued to, since GQL refuses a token sent with another app's client id. Unlinking forgets the token but doesn't revoke it (a pasted one is that browser's sign-in); sign out of Twitch, or disconnect the app under the account's *Connections*, to end it.

The token is stored in `app_state` (key `twitch_viewer_oauth`), never in the config or the logs, and a device-flow token is refreshed before it expires. Anything wrong with it (expired, revoked, Twitch refusing it) only costs the ads: the worker logs a warning and asks for the token anonymously, as without an account. If ads still come through on a token asked for as the account, the recorder logs `stitched ads in the stream although the playback token is <login>'s` once per break: that account isn't ad-free on the channel. `/admin/health`'s `twitchViewer` has the same status as `GET /admin/twitch/viewer` (rechecked at most every 10 minutes). The audit log has `twitch_viewer.link.start`, `twitch_viewer.link` (by whoever started the flow; `failed` with the `error` if Twitch refused), `twitch_viewer.paste` and `twitch_viewer.unlink`, with the account (login, user id, client id, a fingerprint of the token, never the token) as `before`/`after`.

Viewing as a logged-in account is how Twitch's own player works, but whether doing it from a server is within Twitch's terms is the broadcaster's call; leave it unlinked to record as before.
