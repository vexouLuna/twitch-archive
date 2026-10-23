# ADR-0004: Synthetic VODs replace row-moving merge and split

**Status:** Accepted — 2026-10-23 (the plan below is not carried out yet)
**Date:** 2026-10-23
**Deciders:** Project owner

## Context

There are two ways to merge two VODs of one broadcast or to split one in two (docs/admin-api.md, "Merging and
splitting VODs"):

- **Splices** (`/admin/vods/{id}/merge|split|unmerge|unsplit`, `splices.py`, `timeline.py`, migration
  0007) move rows between the VODs: chat, `games`, emotes, chapters and uploads, re-keyed and shifted.
  A `vod_splices` row keeps the snapshot that undoes it. Since the VODs no longer match Twitch's,
  every Twitch-facing step and route refuses a spliced VOD (`refuse_spliced`, the run-time refusals in
  the steps), and an undo must go in reverse order and refuses edits made since.
- **Synthetic VODs** (`/api/v2/vods/{id}/merge|split`, `synthetic.py`, `compose.py`, migration 0016)
  touch neither VOD. They store segments over the real VODs and supersede them: the sources leave the
  lists and redirect into the synthetic VOD. Undo means deleting it. They also cover what splices
  can't: a split anywhere (not only where an upload ends), playthroughs, compiled uploads.

ADR 0003 (step 5) asked whether merge and split should still be offered as splices. The owner's answer
is that synthetic VODs fully replace them.

## Decision

Stop making splices, convert every active splice into the equivalent synthetic VOD(s), then remove
the splice code. Tables and columns stay (migrations stay additive). Each phase is its own pull request.

### Phase 0: inventory (production, read-only)

Run these against production before writing the conversion. Each one changes what the script has to
handle.

```sql
-- 1. Active splices, by kind
SELECT kind, count(*) FROM vod_splices WHERE undone_at IS NULL GROUP BY kind;

-- 2. VODs in more than one active splice: a chain (A+B, then +C; or a merge then a split),
--    converted as one group
SELECT id, count(*) FROM (
  SELECT vod_id AS id FROM vod_splices WHERE undone_at IS NULL
  UNION ALL SELECT other_id FROM vod_splices WHERE undone_at IS NULL) t
GROUP BY id HAVING count(*) > 1;

-- 3. Spliced VODs with censor ranges: set on the spliced timeline, so they would be wrong after an undo
SELECT v.id FROM vods v WHERE v.censor <> '[]'::jsonb AND EXISTS (
  SELECT 1 FROM vod_splices sp WHERE sp.undone_at IS NULL AND v.id IN (sp.vod_id, sp.other_id));

-- 4. Synthetic VODs that play a spliced VOD: their windows are on the spliced timeline
SELECT seg.vod_id AS synthetic, seg.source_id, s.synthetic ? 'auto' AS auto
FROM vod_segments seg JOIN vods s ON s.id = seg.vod_id
WHERE EXISTS (SELECT 1 FROM vod_splices sp
              WHERE sp.undone_at IS NULL AND seg.source_id IN (sp.vod_id, sp.other_id));

-- 5. Spliced VODs edited since their splice: an undo restores the snapshot and loses these edits
SELECT sp.id AS splice, sp.kind, v.id AS vod,
  array_remove(ARRAY[
    CASE WHEN v.title    IS DISTINCT FROM sp.snapshot->'result'->v.id->>'title'    THEN 'title' END,
    CASE WHEN v.duration IS DISTINCT FROM sp.snapshot->'result'->v.id->>'duration' THEN 'duration' END,
    CASE WHEN v.chapters IS DISTINCT FROM sp.snapshot->'result'->v.id->'chapters'  THEN 'chapters' END,
    CASE WHEN v.youtube  IS DISTINCT FROM sp.snapshot->'result'->v.id->'youtube'   THEN 'youtube' END,
    CASE WHEN v.drive    IS DISTINCT FROM sp.snapshot->'result'->v.id->'drive'     THEN 'drive' END
  ], NULL) AS edited
FROM vod_splices sp JOIN vods v ON v.id IN (sp.vod_id, sp.other_id)
WHERE sp.undone_at IS NULL AND sp.snapshot->'result' ? v.id;
```

If query 1 returns nothing, skip phase 2.

**Results (2026-10-23):** one archive has 4 active merges and the other none. There are no splits,
chains or censor ranges. One hand-made synthetic VOD (not `auto`) plays a merge's target, so step 4 below
applies to it. In each merge, only the target's `youtube` list has changed since the merge. That is likely
per-upload data added afterwards (seek-bar `preview`, live `gaps`); the query below shows which keys changed:

```sql
-- per active merge: the upload entries changed since the merge, and which of their keys
SELECT sp.id AS splice, v.id AS vod, (v.id = sp.vod_id) AS is_target, d.ord AS part,
       CASE WHEN d.then_part IS NULL THEN 'added' WHEN d.now_part IS NULL THEN 'removed' ELSE 'changed' END AS what,
       (SELECT array_agg(k ORDER BY k) FROM (
          SELECT k FROM jsonb_object_keys(coalesce(d.now_part, '{}') || coalesce(d.then_part, '{}')) k
          WHERE d.now_part->k IS DISTINCT FROM d.then_part->k) x) AS keys
FROM vod_splices sp
JOIN vods v ON v.id IN (sp.vod_id, sp.other_id)
CROSS JOIN LATERAL (
  SELECT coalesce(n.ord, t.ord) AS ord, n.elem AS now_part, t.elem AS then_part
  FROM jsonb_array_elements(coalesce(v.youtube, '[]')) WITH ORDINALITY n(elem, ord)
  FULL JOIN jsonb_array_elements(coalesce(sp.snapshot->'result'->v.id->'youtube', '[]')) WITH ORDINALITY t(elem, ord)
    ON n.ord = t.ord
) d
WHERE sp.undone_at IS NULL AND sp.snapshot->'result' ? v.id AND d.now_part IS DISTINCT FROM d.then_part
ORDER BY sp.id, v.id, d.ord;
```

### Phase 1: stop making splices

- Port `GET /admin/vods/{id}/merge-candidates` to `/api/v2` (the logic is `splices.merge_candidates`;
  it moves to `synthetic.py`). It is read-only and the dashboard's merge dialog needs it.
- The dashboard (Manage, in the site's repository) uses `/api/v2/vods/{id}/merge|split` and
  `DELETE /api/v2/synthetic/{id}` for undo. That is a change in the site, not here.
- `POST /admin/vods/{id}/merge` and `/split` answer **410** with a pointer to the `/api/v2` route.
  `unmerge` and `unsplit` keep working: phase 2 needs them, and so does anyone who wants to undo by hand.

### Phase 2: convert the active splices

Add a committed, documented command: `archive-worker convert-splices` (docs/operations.md). It is a dry run
unless `--apply` is given, and it is safe to run again: a converted splice is undone, so it is not
picked up a second time.

For each group of connected active splices (query 2), newest splice first:

1. **Refuse and report** (no change) if any VOD of the group has an active job, censor ranges
   (query 3), or edits since the splice (query 5) that the steps below don't carry over. Those are
   resolved by hand, then the command is run again. Changed keys of upload entries that are still
   there (matched by YouTube video id) are carried over (step 3); added or removed entries are not.
2. **Work out the result as segments** from the splice records: a merge of B into A at `offset_s` is
   `[A 0–end at 0, B 0–end at offset]`, which is `compose.merge_segments` with the same offset. A split
   of A at `offset_s` is `[A 0–at]` and `[A at–end]`. A chain composes these into one list per
   resulting VOD.
3. **In one transaction:** undo the group's splices newest first (`splices._undo`, forced once step 1
   has passed). Then put back the per-upload keys that changed since the splice, on the restored
   entry with the same YouTube video id, whichever VOD it went back to. Then create the synthetic
   VOD(s) with `supersedes: true` (`synthetic.create`, changed to take the caller's session for this):
   - merge: id `A+B` (`compose.merge_id`), or `A+B+C` for a chain;
   - split: ids `A-1` and `A-2` (`compose.split_ids`). The split-off VOD was `A-2` already, so its URL
     keeps working, and `A` redirects into `A-1`;
   - title, tags and `thumbnail_url` copied from what the spliced VOD showed (the edits of query 5
     that make sense on the new VOD).
4. **Repoint the synthetic VODs of query 4.** An `auto` playthrough recomputes itself on the next
   monitor round. A hand-made one has its segments mapped from the spliced timeline back onto the
   real VODs; when a window crosses a join it becomes two segments.
5. **Audit** each group as `splice.convert`, with the splice ids and the synthetic ids in `detail`.
   Also write a `synthetic.create` row for each new VOD, as
   `/api/v2` does.

Check on a restored copy first (docs/development.md): a dry run, then `--apply`. Then, for each converted VOD,
check that:

- the site plays the same video;
- chat lines up at a few timestamps;
- old URLs (`/vods/A`, `/vods/B` with `?t=`) redirect to the same moment.

The redirect from a superseded VOD uses the segment's `at` where the old one used
`merged_into.offset`. Confirm that the site applies it with the time it was given.

After the conversion, run `/admin/youtube/parts` for each source, so the YouTube descriptions stop
listing the merged parts.

### Phase 3: remove the splice code

Once query 1 returns nothing:

- **Delete:** `splices.py`, `timeline.plan_merge`, `timeline.plan_split` and what only they use (keep
  `gap_chapter`, `shift`, `clip` and the chapter helpers `compose.py` uses), `tests/worker/test_splices.py`
  and the splice cases of `test_timeline.py`.
- **Remove these routes:** `/admin/vods/{id}/merge|split|unmerge|unsplit|merge-candidates`. Their 410s
  stay for one release, then they go.
- **Remove these refusals:** `refuse_spliced` and the run-time refusals in the steps. No VOD is spliced
  any more, so they can no longer fire.
- **Remove these fields:** `vods.active_splices`, `notify_rows_moved` (if nothing else uses it) and the
  `splices` field of `/api/v2`'s VOD view, together with the dashboard.
- **Keep:** the `vod_splices*` tables (history; dropping them would be a non-additive migration),
  `vods.merged_into` and the public API's handling of it. That is part of the response contract
  (`$merged`, `merged_into` in `tests/api_contract/`), and it costs a few lines. Remove it later
  together with the sites, if wanted.

## Consequences

- **Easier:** one way to merge and split, and it never changes a real VOD. So the Twitch-facing
  steps work on every real VOD again (no "spliced" refusals), and undo is a delete, in any order. Roughly
  1,000 lines of worker code and their tests go.
- **Harder:** a one-off conversion against production data, which needs the inventory, a dry run on a
  restored copy and a check on the site afterwards. Also a dashboard change in the site's repository.
- **Unchanged:** the schema. Splices that were already undone stay in `vod_splices` as history.
