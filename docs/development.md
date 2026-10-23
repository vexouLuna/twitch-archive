# Development

[← README](../README.md)

```
packages/common/archive_common/   settings, DB models, Twitch Helix/GQL clients, http helper
services/api/archive_api/         FastAPI app, Feathers query parser, serializers, comments port
services/worker/archive_worker/   monitor, job runner, steps/, hls, ffmpeg, youtube, admin API
migrations/                       Alembic (0000 legacy baseline, 0001 jobs/app_state/log indexes, 0002 jobs.not_before, 0003 manual step control, … 0007 VOD merges and splits, 0008 bot chat, 0009 admin sessions, 0010 hidden VODs, 0011 runtime settings, 0012 vex-platform jobs schema + audit_log, 0013 job_runs ids, 0014 admin_audit copied into audit_log, 0015 job_runs.scope, 0016 synthetic VODs, 0017 job_runs.parent_id, 0018 site tags, 0019 censor ranges, 0020 tag types, 0021 cut reason)
tests/api_contract/               golden responses from the legacy API + replay tests
tests/worker/                     HLS parsing, planning, capture (respx), ffmpeg, DB-backed steps/runner
deploy/                           roles.sql, which compose's roles step runs
secrets.example/                  the secrets files compose mounts
Dockerfile                        both images, as targets: --target api, --target worker
docs/                             the manual (README links each page)
docs/adr/                         architecture decisions
```

```bash
uv run pytest                     # all tests; DB tests skip without Postgres, ffmpeg tests skip without ffmpeg
uv run pytest tests/worker -q
uv run ruff check && uv run ruff format --check && uv run mypy   # what CI's lint job runs
```

mypy runs in strict mode. The code written before it was turned on still has inline
`# type: ignore[...]` comments for each error it found then; remove one when you touch that code.

The DB-backed tests use `ARCHIVE_DATABASE_URL`. The default is the `compose.dev.yaml` database. They expect `alembic upgrade head` to have been run, and no other queued jobs in that database. An empty, migrated database is enough for all of them except `tests/api_contract`, which replays responses captured from the live data and skips unless a matching dump is restored; CI runs them that way (`ci.yml`).

**Re-capturing the golden API responses** (only needed if the frontend starts using new queries). This reads from the old Node API:

```bash
uv run python tests/api_contract/capture_golden.py http://legacy-host:3030
```

Then restore a matching dump locally (`pg_dump -Fc archive` on the server, then `pg_restore` as in [Quick start (local development)](../README.md#quick-start-local-development)) so the replay compares against the same data.

**Conventions:**
- The runner saves `ctx.payload` after each step returns, so a finished step is never re-run. A step interrupted part-way is re-run, so long steps save their own progress in `ctx.payload` with `ctx.save()` and skip work already done.
- Every ffmpeg output is written to `*.part` and renamed on success.
- Pure logic lives in `planning.py` and `hls.py`, so it can be tested without I/O.
