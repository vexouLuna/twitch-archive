"""The ``censor_check`` step: IGDB's content descriptors for the games a VOD is about to upload.

Twitch's categories are IGDB games (Helix ``/games`` gives each one's ``igdb_id``), and IGDB lists
each game's age-rating content descriptors ("Sexual Content", "Partial Nudity", ...) and themes.
Twitch allows a game whose rating carries them; YouTube may not. A game is decided like this, first
match wins:

1. in ``restricted_games``: cut (``split`` leaves its chapters out anyway; not looked up);
2. in ``censor_allow_games``: uploaded as it is (not looked up);
3. a descriptor or theme containing one of ``censor_descriptors`` (case-insensitive): review;
4. otherwise uploaded.

On a review the job pauses before ``censor`` with the games in ``payload["censor_review"]``. The
admin then sets censor ranges and resumes, resumes as it is, or cuts the games
(POST /admin/jobs/{id}/censor-review). An IGDB error pauses it the same way: unchecked is not clean.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from vex_platform.actor import Actor

from .. import planning
from ..context import JobContext, StepError
from .metadata import file_chapters

Decision = Literal["cut", "upload", "review"]


def descriptors(game: dict[str, Any] | None) -> list[str]:
    """An IGDB game's content descriptors (of every age rating) and theme names, without repeats."""
    if not game:
        return []
    found = [
        d.get("description")
        for rating in game.get("age_ratings") or []
        for d in rating.get("rating_content_descriptions") or []
    ]
    found += [t.get("name") for t in game.get("themes") or []]
    return list(dict.fromkeys(f for f in found if isinstance(f, str) and f))


def matching(found: list[str], blocking: list[str]) -> list[str]:
    """The descriptors that contain one of ``blocking`` (case-insensitive: "nudity" matches "Partial Nudity")."""
    keys = [b.casefold() for b in blocking if b.strip()]
    return [f for f in found if any(k in f.casefold() for k in keys)]


def decide(name: str | None, restricted: list[str], allowed: list[str], hits: list[str]) -> Decision:
    """The order in the module docstring: the cut list wins over the allow list, which wins over IGDB."""
    if planning.restricted_game(name, restricted):
        return "cut"
    if name in allowed:
        return "upload"
    return "review" if hits else "upload"


def games_to_check(chapters: list[dict[str, Any]] | None, restricted: list[str], allowed: list[str]) -> dict[str, str]:
    """Twitch game id -> name of the chapters that would be uploaded and are not on either list."""
    out: dict[str, str] = {}
    for ch in chapters or []:
        game_id, name = ch.get("gameId"), ch.get("name")
        if game_id and not ch.get("restricted") and decide(name, restricted, allowed, ["?"]) == "review":
            out[str(game_id)] = name or ""
    return out


def cut_games(chapters: list[dict[str, Any]] | None, game_ids: set[str]) -> list[dict[str, Any]]:
    """``chapters`` with those of ``game_ids`` marked restricted, so ``split`` leaves them out."""
    return [ch | {"restricted": True} if str(ch.get("gameId")) in game_ids else ch for ch in chapters or []]


async def flagged_games(ctx: JobContext, games: dict[str, str]) -> list[dict[str, Any]]:
    """``games`` whose IGDB entry has a blocking descriptor: ``{gameId, name, igdbId, matched}`` each. Logs what
    IGDB had for each game, so a descriptor that should block (or a game IGDB doesn't know) shows in the job log."""
    helix = ctx.deps.helix
    blocking = ctx.settings.censor_descriptors
    igdb_ids: dict[str, int] = {}
    for game_id, name in games.items():
        igdb_id = ((await helix.get_game(game_id)) or {}).get("igdb_id")
        if igdb_id and str(igdb_id).isdigit():
            igdb_ids[game_id] = int(igdb_id)
        else:
            ctx.log.warning(
                "%s (Twitch %s) has no IGDB entry, so it was not checked; add it to censor_allow_games or "
                "restricted_games to decide it yourself",
                name or "?",
                game_id,
            )
    found = await helix.igdb_games(list(igdb_ids.values())) if igdb_ids else {}
    out = []
    for game_id, igdb_id in igdb_ids.items():
        listed = descriptors(found.get(igdb_id))
        hits = matching(listed, blocking)
        ctx.log.info(
            "%s (IGDB %d): %s; %s",
            games[game_id],
            igdb_id,
            f"descriptors and themes: {', '.join(listed)}" if listed else "no descriptors or themes",
            f"matches {', '.join(hits)}" if hits else "nothing matches censor_descriptors",
        )
        if hits:
            out.append({"gameId": game_id, "name": games[game_id], "igdbId": igdb_id, "matched": hits})
    return out


def explain_skips(ctx: JobContext, chapters: list[dict[str, Any]] | None) -> None:
    """Debug lines for the chapters' games the check leaves alone, and why."""
    s = ctx.settings
    seen: set[str] = set()
    for ch in chapters or []:
        name = ch.get("name")
        key = str(ch.get("gameId") or name)
        if key in seen:
            continue
        seen.add(key)
        if ch.get("restricted") or planning.restricted_game(name, s.restricted_games):
            ctx.debug("%s: cut from the upload (restricted), not checked", name or "no category")
        elif name in s.censor_allow_games:
            ctx.debug("%s: in censor_allow_games, uploaded as it is", name)
        elif not ch.get("gameId"):
            ctx.debug("%s: no Twitch category id, not checked", name or "no category")


async def censor_check(ctx: JobContext) -> None:
    s = ctx.settings
    if not s.censor_descriptors:
        ctx.log.info("censor check off: censor_descriptors is empty")
        return
    chapters = file_chapters(ctx, await ctx.get_vod())
    explain_skips(ctx, chapters)
    games = games_to_check(chapters, s.restricted_games, s.censor_allow_games)
    if not games:
        ctx.log.info("censor check: no game left to look up")
        return
    if not ctx.deps.helix.configured:
        ctx.log.warning("no Twitch app configured; IGDB not checked for %s", ", ".join(games.values()))
        return
    ctx.debug("censor_descriptors: %s", ", ".join(s.censor_descriptors))
    try:
        flagged = await flagged_games(ctx, games)
    except httpx.HTTPError as exc:
        flagged = [{"gameId": None, "name": None, "igdbId": None, "matched": [], "error": f"IGDB lookup failed: {exc}"}]
    if not flagged:
        ctx.log.info("IGDB: nothing in %s matches %s", ", ".join(games.values()), ", ".join(s.censor_descriptors))
        return
    ctx.payload["censor_review"] = {"games": flagged}
    what = "; ".join(g.get("error") or f"{g['name']} ({', '.join(g['matched'])})" for g in flagged)
    if ctx.run is None:
        raise StepError(f"needs a censor review: {what}")  # a legacy job cannot pause itself
    ctx.log.warning("needs a censor review before upload: %s", what)
    # Running, so it pauses at the next step boundary: before censor.
    await ctx.run.runtime.pause(ctx.job_id, actor=Actor("job", str(ctx.job_id), ctx.kind, "job"))
