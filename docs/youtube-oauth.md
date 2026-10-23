# YouTube OAuth setup

[← README](../README.md)

The worker needs a refresh token for the YouTube channel. There are two ways to get one.

## Option A: import the token the old app already has

```bash
# from the stack directory; /path/to/archive is the old app's checkout
docker compose run --rm -v /path/to/archive/config:/legacy:ro worker \
  archive-worker import-youtube-token /legacy/config.json
```

This copies `youtube.auth.refresh_token` into the `app_state` table. `ARCHIVE_GOOGLE_CLIENT_ID` and `ARCHIVE_GOOGLE_CLIENT_SECRET` must be the same Google client the old app used; they are `google.client_id` and `google.client_secret` in the old config.

## Option B: run the consent flow again from the dashboard

When the admin API is published behind the site's proxy (vods.vexoul.net serves it at `/backend-admin`), Google can redirect straight back through it:

1. In Google Cloud Console → Credentials → your OAuth client, add the authorized redirect URI `https://<site>/backend-admin/admin/refreshtoken` (e.g. `https://vods.vexoul.net/backend-admin/admin/refreshtoken`).
2. Set `ARCHIVE_GOOGLE_REDIRECT_URL` to the same URI and restart the worker.
3. On the site, Manage → Overview → **Connect YouTube**, sign in with the channel's Google account and allow access. The tab ends on "YouTube authorized. You can close this tab.", and the YouTube tile turns valid.

`/admin/refreshtoken` needs no key or session: the `state` proves the request. It is random, issued by `/admin/youtube/auth`, works once, and expires after 15 minutes or when the worker restarts (start the flow again then). The callback answers with or without a trailing slash, so the old app's `/admin/refreshtoken/` URI works too.

## Option C: run the consent flow through an SSH tunnel

Google only accepts `http://` redirect URIs for `localhost`, so without the proxy, open an SSH tunnel to the worker's admin port and do the flow in your own browser:

1. In Google Cloud Console → Credentials → your OAuth client, make sure an authorized redirect URI matches `ARCHIVE_GOOGLE_REDIRECT_URL`. The old app registered `http://localhost:3030/admin/refreshtoken/`. You can either reuse it (set `ARCHIVE_GOOGLE_REDIRECT_URL` to it and tunnel local port 3030) or add `http://localhost:3031/admin/refreshtoken`.
2. Open the tunnel: `ssh -L 3031:127.0.0.1:3031 user@your-server` (use `-L 3030:127.0.0.1:3031` if you reuse the old URI).
3. Get the consent URL:
   ```bash
   curl -s -H "Authorization: Bearer $KEY" http://localhost:3031/admin/youtube/auth
   ```
4. Open the `url` from the response, sign in with the channel's Google account and allow access. Google then redirects to `/admin/refreshtoken`, which stores the token. The `state` is HMAC-signed with the admin key and expires after 15 minutes.
5. Check it worked: `curl -s -H "Authorization: Bearer $KEY" http://localhost:3031/admin/youtube/status` should return `"valid": true`. The callback also runs this check and returns an error if the stored token can't be used.

## Keeping the token alive

The worker refreshes the access token on its own before every upload, but the **refresh token** can still stop working:

- **Six months without use.** Google revokes a refresh token that hasn't been used for six months. The old app only refreshed the token when it uploaded, so a channel that stopped streaming for six months lost it.
- **Testing mode.** While the OAuth consent screen is in "Testing", refresh tokens expire after 7 days. Set the app to "In production" (a personal app with the `youtube` scope works unverified; you just click through the "unverified app" warning once).
- **Revoked by hand**, a Google password change, or more than 100 refresh tokens issued for the same client and account.

To handle the first case and detect the others early, the worker has a keep-alive task. It runs while `ARCHIVE_YOUTUBE_UPLOAD` is on and a Google client is configured. It refreshes the token at startup and then every `ARCHIVE_YOUTUBE_KEEPALIVE_HOURS` (default 24). Each run logs one line:

- `YouTube token refreshed (keep-alive)`: all good.
- `ERROR ... YouTube token is not usable (RefreshError: invalid_grant ...)`: run Option B again before the next stream ends.

`GET /admin/youtube/status` does the same refresh on demand and returns `{"authorized": true, "valid": true, "accessTokenExpiry": "...", "channel": {"id": "UC…", "title": "...", "url": "https://www.youtube.com/@…"}}`, or `{"authorized": ..., "valid": false, "error": "..."}`. `authorized` means a refresh token is stored. `valid` means Google accepted it just now and the account has a YouTube channel to upload to: `channel` is that channel (one quota unit per check), and `null` with `valid: false` when the account has none, which uploads would refuse with `youtubeSignupRequired`. If the channel lookup itself fails (quota, network), `channel` is left out and `valid` stands. `connectedAt` is when the account was connected and `refreshTokenExpiresAt` when Google said its refresh token ends (sent only for a time-limited grant, else `null`); both are `null` for a token imported from the legacy config. `/admin/health`'s `youtube` carries the same `channel`, `connectedAt` and `refreshTokenExpiresAt`.

Every token refresh is saved back to `app_state`: the new access token, the scopes Google granted (`grantedScopes`) and `refreshedAt`. That covers the refreshes google-auth makes by itself, such as an access token that expires during an upload, not just these checks. If Google answers a refresh with a new refresh token, it replaces the old one, `refreshTokenRotatedAt` dates it and the worker logs `Google sent a new YouTube refresh token on refresh (<old> -> <new>)` (fingerprints, never the tokens). Each refresh is an audit row, `youtube.token.refresh` (actor `system`, target `youtube`): `detail` has the `reason` (`check` for a forced refresh, `expired` for one google-auth made), `changed` (`refreshToken`, `grantedScopes`, or empty) and `accessTokenExpiry`, and `before`/`after` hold the changed values. A refresh Google refuses is a `failed` row with the `error`. Connecting an account through the OAuth flow is a `youtube.connect` row, by whoever opened `/admin/youtube/auth`, with the old and new token's summary (refresh-token fingerprint, `connectedAt`, `refreshTokenExpiresAt`, `grantedScopes`, `channel`) as `before`/`after`.
