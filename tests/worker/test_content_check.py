from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from archive_worker.context import StepError
from archive_worker.planning import chapter
from archive_worker.steps import content_check as cc
from pydantic import SecretStr

HELIX_GAMES = "https://api.twitch.tv/helix/games"
IGDB_GAMES = "https://api.igdb.com/v4/games"
TOKEN = "https://id.twitch.tv/oauth2/token"

LEWD = {
    "id": 11,
    "themes": [{"name": "Action"}, {"name": "Erotic"}],
    "age_ratings": [
        {"rating_content_descriptions": [{"description": "Partial Nudity"}, {"description": "Violence"}]},
        {"rating_content_descriptions": [{"description": "Strong Sexual Content"}, {"description": "Violence"}]},
    ],
}
CLEAN = {"id": 22, "themes": [{"name": "Puzzle"}], "age_ratings": [{"rating_content_descriptions": []}]}
BLOCKING = ["Sexual Content", "Nudity"]


def test_descriptors_of_every_rating_and_theme_without_repeats():
    assert cc.descriptors(LEWD) == ["Partial Nudity", "Violence", "Strong Sexual Content", "Action", "Erotic"]
    assert cc.descriptors(None) == [] and cc.descriptors({"id": 1}) == []


def test_matching_is_a_case_insensitive_substring():
    assert cc.matching(cc.descriptors(LEWD), BLOCKING) == ["Partial Nudity", "Strong Sexual Content"]
    assert cc.matching(cc.descriptors(LEWD), ["erotic"]) == ["Erotic"]
    assert cc.matching(cc.descriptors(CLEAN), BLOCKING) == []
    assert cc.matching(["Nudity"], ["", " "]) == []


def test_cut_list_beats_allow_list_beats_igdb():
    hits = ["Nudity"]
    assert cc.decide("Game", ["Game"], ["Game"], hits) == "cut"
    assert cc.decide("Game", [], ["Game"], hits) == "upload"
    assert cc.decide("Game", [], [], hits) == "review"
    assert cc.decide("Game", [], [], []) == "upload"
    assert cc.decide(None, ["No category"], [], hits) == "cut"


def test_only_uploaded_unlisted_games_are_looked_up():
    chapters = [
        chapter("1", "Cut", None, 0, 10, False),
        chapter("2", "Allowed", None, 10, 10, False),
        chapter("3", "Lewd", None, 20, 10, False),
        chapter("3", "Lewd", None, 30, 10, False),
        chapter("4", "Already cut", None, 40, 10, True),
        chapter(None, None, None, 50, 10, False),
    ]
    assert cc.games_to_check(chapters, ["Cut"], ["Allowed"]) == {"3": "Lewd"}


def test_cut_games_marks_their_chapters_restricted():
    chapters = [chapter("3", "Lewd", None, 0, 10, False), chapter("5", "Clean", None, 10, 10, False)]
    assert [c["restricted"] for c in cc.cut_games(chapters, {"3"})] == [True, False]


def _ctx(make_ctx, settings, monkeypatch, chapters, run=None):
    settings.twitch_client_id = "cid"
    settings.twitch_client_secret = SecretStr("not-a-real-secret")
    ctx = make_ctx("archive", "100")
    ctx.run = run

    async def get_vod() -> Any:
        return SimpleNamespace(chapters=chapters)

    monkeypatch.setattr(ctx, "get_vod", get_vod)
    respx.post(TOKEN).mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
    respx.get(HELIX_GAMES).mock(
        side_effect=lambda r: httpx.Response(
            200, json={"data": [{"id": r.url.params["id"], "igdb_id": "11" if r.url.params["id"] == "3" else "22"}]}
        )
    )
    return ctx


@respx.mock
async def test_a_flagged_game_pauses_the_job_before_censor(make_ctx, settings, monkeypatch):
    paused: list[int] = []

    async def pause(run_id: int, actor: Any) -> None:
        paused.append(run_id)

    run = SimpleNamespace(payload={}, log=logging.getLogger("test"), run_id=7, runtime=SimpleNamespace(pause=pause))
    chapters = [chapter("3", "Lewd", None, 0, 10, False), chapter("5", "Clean", None, 10, 10, False)]
    ctx = _ctx(make_ctx, settings, monkeypatch, chapters, run)
    igdb = respx.post(IGDB_GAMES).mock(return_value=httpx.Response(200, json=[LEWD, CLEAN]))
    await cc.censor_check(ctx)
    assert "where id = (11,22); limit 2;" in igdb.calls[0].request.content.decode()
    assert igdb.calls[0].request.headers["client-id"] == "cid"
    assert paused == [ctx.job_id]
    assert ctx.payload["censor_review"] == {
        "games": [{"gameId": "3", "name": "Lewd", "igdbId": 11, "matched": ["Partial Nudity", "Strong Sexual Content"]}]
    }
    # Cached: a second check does not ask IGDB again.
    await cc.censor_check(ctx)
    assert igdb.call_count == 1


@respx.mock
async def test_allow_list_and_clean_games_upload(make_ctx, settings, monkeypatch):
    settings.censor_allow_games = ["Lewd"]
    ctx = _ctx(
        make_ctx,
        settings,
        monkeypatch,
        [chapter("3", "Lewd", None, 0, 10, False), chapter("5", "Clean", None, 10, 10, False)],
    )
    igdb = respx.post(IGDB_GAMES).mock(return_value=httpx.Response(200, json=[CLEAN]))
    await cc.censor_check(ctx)
    assert "where id = (22);" in igdb.calls[0].request.content.decode()
    assert "censor_review" not in ctx.payload


@respx.mock
async def test_an_igdb_error_needs_a_review_too(make_ctx, settings, monkeypatch):
    ctx = _ctx(make_ctx, settings, monkeypatch, [chapter("3", "Lewd", None, 0, 10, False)])
    respx.post(IGDB_GAMES).mock(return_value=httpx.Response(500))
    with pytest.raises(StepError, match="IGDB lookup failed"):  # a legacy job (no run) cannot pause
        await cc.censor_check(ctx)


async def test_no_descriptors_turns_the_check_off(make_ctx, settings):
    settings.censor_descriptors = []
    await cc.censor_check(make_ctx())  # would need the database for the VOD otherwise
