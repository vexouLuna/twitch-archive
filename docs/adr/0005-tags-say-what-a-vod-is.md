# ADR-0005: Tags say what a VOD is; merges are ordinary synthetic VODs

**Status:** Accepted — 2026-10-24
**Date:** 2026-10-24
**Deciders:** Project owner

## Context

A VOD's place on the site comes from three different things today:

- **Untagged** VODs are the "plain" ones: `/vods`, the homepage's `latest` card and the API's default
  list (`scope` in `services.py`) are the VODs with no tags.
- **Tags with a type** (migration 0020): a tag's `page` gives it a list page and a header link, and its
  `homeCard` lets /manage/home add a "latest" card for it. The two are separate switches. `compilation`
  is set when a playthrough is made and carries the Playthroughs page and card.
- **`supersedes`** on a synthetic VOD (a merge or split): its sources leave every list, `GET /vods/{id}`
  answers for them with `superseded_by`, and the site redirects. It also changes how streams are numbered
  and stops two superseding VODs from covering the same second.

Problems the owner raised:

1. Turning off a tag's `homeCard` doesn't remove its card from the homepage. The switch only decides
   whether /manage/home can add the card. A saved (or default) `homeCards` keeps it.
2. "Plain" means "has no tags", so a plain VOD can't carry a tag without leaving `/vods`. A merge only
   counts as one by being untagged and superseding.
3. Merges and splits carry behaviour that other synthetic VODs don't have.

## Decision

### One switch: a tag is a "home category" or not

A tag with a `page` is a **home category**. It has a list page, a header link, and can be a homepage
card. `homeCard` is no longer set on its own: the archive stores `homeCard = page is not null`, so sites
that still read it get the right answer. /manage/tags shows one "Home category" switch, which sets the
page (defaulting to the tag's name) and its title.

A homepage card for a tag that isn't a home category is never shown. `site_options.load` drops it
(the API and the dashboard both read through it), and the site drops it again against the tags it has
(problem 1, and an archive that never saved its options still gets the default cards checked).

### Auto tags

| Tag | Set by | Means |
|---|---|---|
| `new`, `updated` | the site, from dates (never stored) | as before |
| `vods` | the archive, on every real VOD when its row is made (`vods.upsert_vod`), live or not; on a merge or split when it's made | "a VOD": listed on `/vods` |
| `compilation` | the archive, on a synthetic VOD exactly while it keeps itself up to date (`synthetic.auto`, "Keep up to date") | "rebuilds itself" |

`vods` is a home category (page `vods`, the only tag allowed that reserved page). `/vods` lists the VODs
tagged `vods`, the same way `/playthroughs` lists a tag's VODs. `compilation` stops being a home
category. A new plain tag, **`playthroughs`**, takes over its page, card and look. Like any other tag,
it is set by hand (the playthrough builder adds it by default).

The `vods` and `compilation` tags don't show as chips on a card. They say what a VOD is, so a chip
would be on nearly every card. A site can still draw them on the thumbnail.

### Homepage cards

| Card | Shows |
|---|---|
| `{kind: "latest"}` | "Latest live": the stream while live, else the newest `vods` VOD (unchanged) |
| `{kind: "one", tag}` | "Latest VOD": the newest VOD of a home category, one big card, never the stream |
| `{kind: "tag", tag}` | "Latest 2": the two newest of a home category (unchanged) |
| `{kind: "games"}` | the most played games (unchanged) |

The three existing kinds keep their stored shape and meaning. Older sites show the same homepage, and
the migration only renames `tag:compilation` to `tag:playthroughs`.

### Merges and splits are ordinary synthetic VODs

`supersedes` is gone. A merge or split is a synthetic VOD tagged `vods`. What used to stand in for its
sources is now the sources being **hidden**: hidden VODs leave every list, as before. Merge and split
hide their sources as part of the action. The synthetic VOD's manage page can show or hide all of its
sources at once.

The hidden-VOD rule changes for one case. A hidden VOD that a shown synthetic VOD plays (at any depth)
is still answered by `GET /vods/{id}`, its chat, games rows and emotes, because the synthetic VOD can't
play without them. Its JSON then has `"hidden": true` and:

- `superseded_by` (as before: the segments, for mapping `?t=`) when exactly **one** shown synthetic VOD
  plays it directly. The site redirects there, and so do older sites.
- `appears_in` when **two or more** do. The site shows a page listing them.

A hidden VOD that no shown synthetic VOD plays answers 404, as before. Lists never show hidden VODs.
Nothing new becomes public: a shown synthetic VOD already lists its sources' ids and plays their
footage.

An `auto` playthrough takes its windows from shown real VODs and from hidden ones that a shown `vods`
synthetic VOD plays (a merge's sources). Without that, the migration would remove merged footage from
playthroughs.

`PUT/POST /api/v2/synthetic` still take `supersedes`. `true` now means "hide the sources and add
`vods`" (what it used to amount to), so older dashboards keep working. Responses keep
`supersedes: false`. `$superseded=true` on `/vods` is accepted and does nothing.

### Merge any number of VODs; pick the ids

- `GET /api/v2/vods/{id}/merge-candidates?hours=24` returns VODs that start less than `hours` after this
  one ends, or end less than `hours` before it starts. Each has `gap`, the seconds between the earlier
  one's end and the later one's start (negative when they overlap). Without `hours`, the old answer.
- `POST /api/v2/vods/{id}/merge {sources: [...], gaps?: [...], id?}` merges this VOD and the sources in
  start order. `gaps[i]` is the gap before the (i+1)-th VOD, or null to place it where it started. `id`
  defaults to `<first id>-m`. The old `{source, gap?}` still makes `A+B`.
- `POST /api/v2/vods/{id}/split {at, ids?}`: `ids` defaults to `[<id>-1, <id>-2]`.

### Synthetic thumbnails

A synthetic VOD's thumbnail is the first of its sources, in order, that has one (it used to be the first
source's, even when that one had none).

## Migration (0022, data only)

1. Tags list, if saved: `homeCard = page is not null`. A tag that had a card but no page gets a page, its
   name, when that's free and not reserved. `playthroughs` is added with `compilation`'s look, page and
   card, unless a `playthroughs` tag exists already. `compilation` loses its page and card. `vods` is added
   first, with page `vods`, title "VODs", label "VOD", not drawn.
2. Options, if saved: `tag:compilation` cards become `tag:playthroughs`.
3. VODs: every real VOD gets `vods`. Every VOD tagged `compilation` gets `playthroughs`. Every synthetic
   VOD with `auto` gets `compilation`.
4. Every synthetic VOD with `supersedes: true`: its direct sources are hidden, it gets `vods`, and
   `supersedes` becomes false.

`compilation` is **not** taken off synthetic VODs without `auto`. Older sites list playthroughs by
`compilation` until they are bumped, so taking it off would empty their Playthroughs page. The next save
of such a VOD drops it, because the archive sets `compilation` to match `auto`.

The downgrade does nothing. Code older than this release reads the new rows as described under
"Rollout".

## Rollout

Backend first, then sites.

- **An older site against the new archive** shows the same homepage. Its `/vods` asks for the default
  list. The default list is now "VODs with no tags other than `vods`", which after the migration is what
  "untagged" used to return. Its Playthroughs page still finds `compilation`, and its header gets
  `playthroughs` from the tags list. A merged VOD's link still redirects, through `superseded_by`. One
  difference: a hidden VOD played by two or more synthetic VODs shows its own page (older sites don't
  know `hidden`) instead of the list.
- **An older dashboard** works. Its "stands in" switch is the legacy `supersedes` above. Its "can be a
  home card" switch is ignored, because the page decides. A card kind it doesn't know (`one`) is dropped
  if it saves /manage/home.
- **The window between the migration and the new worker starting:** a VOD row made by the old worker
  has no `vods` tag, so it is missing from the new `/vods` until tagged by hand. Real VOD rows are made
  once per stream, so this is unlikely during a deploy.

## Consequences

- One rule decides what a list shows: a tag. `/vods` is no longer "everything else".
- `segments.flatten` still reads `supersedes`. After the migration it's always false, so a playthrough
  that plays a merge counts each of the merge's streams. The parameter can go in a later cleanup.
- Hiding a source of a shown synthetic VOD now leaves that source reachable by id, with
  `hidden: true`. To take its footage off the site, hide or change the synthetic VOD as well.
