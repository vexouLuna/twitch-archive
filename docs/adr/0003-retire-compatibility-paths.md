# ADR-0003: Retire the compatibility paths kept beside their replacements

**Status:** Proposed — 2026-10-21 (needs the project owner's decision)
**Date:** 2026-10-21
**Deciders:** Project owner

## Context

Several subsystems were replaced without removing what they replaced, so both run side by side. Each
pair doubles the code paths, tests and docs that have to stay coherent, and none of them says when the
old side may go. The README's "stays until their clients have moved" is the only exit condition, and
nothing measures it.

| Pair | Old side | New side | What still keeps the old side |
|---|---|---|---|
| Jobs | `legacy_jobs.py` runner over `public.jobs` | vex-platform `JobRuntime` (`jobs.job_runs`) | Rows queued before the runtime existed; `job_rows.ALL_JOBS` unions both; `JobService` acts on either |
| Admin API | `/admin/*` (camelCase, `{error, msg}`) | `/api/v2/*` (snake_case, problem details) | The dashboard and scripts; settings and storage are implemented twice, VODs partly |
| Merge and split | `splices.py` (moves rows; `vod_splices*` tables) | Synthetic VODs (`synthetic.py`, `compose.py`) | Existing splices are stored data; the `/admin/vods/*/merge\|split` routes |
| Site sign-in | `GET /admin/signin?quiet=1` redirect | `POST /admin/session {code}` | The sites, until they all use the code flow |
| Auth header | `<any word> <key>` | `Bearer <key>` | Old scripts |
| Job conflict wording | `_v1_conflicts()` rewording vex-platform's messages | vex-platform's own messages | `/admin/jobs` clients that match the old text |

(The startup copy of `admin_audit` into `audit_log` was the seventh. It went once its one-release
window had long passed.)

## Decision (proposed)

Retire the old side of each pair when its exit check holds, in this order. Each retirement is its own
pull request, and none changes a migration (they stay additive: tables stay, code goes).

1. **Legacy job runner.** Exit: no active rows in the old table.
   ```sql
   SELECT count(*) FROM public.jobs WHERE state IN ('queued', 'running', 'paused');
   ```
   Then remove `legacy_jobs.py`, the `JobService` branches for legacy ids, `ALL_JOBS`' legacy half and the
   runner task in `main.py`. Finished legacy rows can stay readable through a plain query if wanted.
   **Done** (the check found no active rows in production): the runner and every action on an old job
   are gone. `ALL_JOBS` keeps its read-only legacy half, so the finished old jobs still list with their
   logs; an action on one answers 409.
2. **Quiet sign-in.** Exit: no site requests `GET /admin/signin?quiet=1` (the sites' repositories), and
   the worker log shows no quiet sign-ins for 30 days.
   **Done:** the sites' code doesn't send it and the log showed none. The owner chose not to wait the
   full 30 days. `quiet=1` is ignored now, so a stray one is an ordinary sign-in.
3. **Duplicated `/admin` settings and storage routes.** Exit: none of their writes in 30 days.
   ```sql
   -- v1 rows come from audit.route_entry without a request id; /api/v2 rows always carry one
   SELECT action, count(*) FROM audit_log
   WHERE at > now() - interval '30 days' AND request_id IS NULL
     AND action LIKE ANY (ARRAY['setting.%', 'storage.%'])
   GROUP BY action;
   ```
   Reads have no audit rows, so check the dashboard's code for `GET /admin/settings` and `/admin/storage` too.
4. **`/admin` VOD routes and conflict rewording.** Exit: `/api/v2` covers games, chapters, YouTube and Drive
   lists, and the dashboard uses it. `_v1_conflicts()` went first: production checks found no client that
   matches the old wording (the dashboard shows the message as it is), so `/admin/jobs` now answers with
   vex-platform's own messages.
5. **Row-moving merge and split.** Decided: synthetic VODs replace them. The plan (stop making them,
   convert the active splices, remove the code) is ADR 0004.
6. **Legacy auth header.** Exit: every script sends `Bearer`. Accepted: the worker logs a warning for any
   other word (once per word per process) for one release, and the release after that accepts only `Bearer`.

## Status (production checks, 2026-10-23)

Run read-only on both production archives.

| Step | Result | Next |
|---|---|---|
| 1. Legacy runner | No active rows (a few finished ones) | **Done** |
| 2. Quiet sign-in | No site sends `quiet=1`; the sites' quiet check already uses `POST /admin/session {code}`. The worker log showed no quiet sign-ins, but covered only 4 days (the containers were recreated 2026-10-19), and the proxy keeps no access log | Removed without waiting for 30 days (owner's call) |
| 3. Settings and storage | Written through the old routes in the last 30 days, all by the dashboard, which still calls `GET`/`PATCH`/`DELETE /admin/settings` and `GET`/`DELETE /admin/storage` | The dashboard moves to `/api/v2/settings` and `/api/v2/storage` (a site change; both exist) |
| 4. VOD routes | The dashboard calls most `/admin/vods/{id}/*` routes; games, chapters and the VOD `PATCH` were written in the last 30 days. No scripts; nothing matches on error text | Games, chapters, YouTube, Drive and censor are in `/api/v2` (`PATCH /api/v2/vods/{id}` already was). Next: emotes, re-archive, the YouTube delete and the censor review; then the dashboard. `_v1_conflicts()`: removed |
| 5. Merge and split | 4 active merges, on one archive (ADR 0004, phase 0) | ADR 0004 |
| 6. Auth header | No API-key writes through the old routes in 30 days | The warning ships; `Bearer` only the release after |

## Consequences

- **Easier:** fewer concepts to keep coherent. `create_admin_app` shrinks, auditing has one fewer writer,
  and the docs stop documenting two of everything.
- **Harder:** each step needs its exit check run against the live database or the sites first. Those are
  owner actions.
- **Unchanged:** stored data and the schema. Rolling back a retirement is reverting its pull request.
