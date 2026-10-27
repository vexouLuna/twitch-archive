"""Validation of hand-edited chapters, YouTube and Drive lists (pure functions)."""

import datetime as dt
import re

import pytest
from archive_worker import vod_edits

TEMPLATE = "https://static-cdn.jtvnw.net/ttv-boxart/509658-{width}x{height}.jpg"


def ch(start, length, name="Just Chatting", **extra):
    return {
        "name": name,
        "gameId": "509658",
        "imageTemplate": TEMPLATE,
        "start": start,
        "length": length,
        "restricted": False,
        **extra,
    }


def test_vod_fields():
    out = vod_edits.vod_fields(
        {
            "title": " t ",
            "hidden": True,
            "thumbnailUrl": "",
            "duration": "123:04:05",
            "createdAt": "2026-09-30T18:00:00Z",
        }
    )
    assert out == {
        "title": "t",
        "hidden": True,
        "thumbnail_url": None,
        "duration": "123:04:05",
        "created_at": dt.datetime(2026, 9, 30, 18, tzinfo=dt.UTC),
    }
    assert vod_edits.vod_fields({"duration": "1:02:03"}) == {"duration": "01:02:03"}
    assert vod_edits.vod_fields({}) == {}
    reason = vod_edits.vod_fields({"cutReason": "  DMCA claim on the music  "})
    assert reason == {"cut_reason": "DMCA claim on the music"}
    assert vod_edits.vod_fields({"cutReason": "   "}) == {"cut_reason": None}
    assert vod_edits.vod_fields({"cutReason": None}) == {"cut_reason": None}


@pytest.mark.parametrize(
    "body, message",
    [
        ({"views": 1}, "unknown field(s) views"),
        ({"title": "  "}, "title must be a non-empty string"),
        ({"hidden": 1}, "hidden must be true or false"),
        ({"thumbnailUrl": "//cdn/x.jpg"}, "thumbnailUrl must be an http(s) URL or null"),
        ({"thumbnailUrl": 5}, "thumbnailUrl must be a URL or null"),
        ({"duration": 5400}, "duration must be HH:MM:SS"),
        ({"duration": "1:2:3"}, "duration must be HH:MM:SS"),
        ({"createdAt": "2026-09-30 18:00"}, "createdAt must be an ISO date and time with an offset"),
        ({"cutReason": 3}, "cutReason must be text or null"),
        ({"cutReason": "x" * 501}, "cutReason must be at most 500 characters"),
    ],
)
def test_vod_fields_refused(body, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        vod_edits.vod_fields(body)


def test_tags():
    assert vod_edits.vod_fields({"tags": [" Compilation", "compilation"]}) == {"tags": ["compilation"]}
    assert vod_edits.vod_fields({"tags": []}) == {"tags": []}
    assert vod_edits.tags(["Complete", "x"], ("x", "complete")) == ["complete", "x"]
    with pytest.raises(ValueError, match=re.escape("unknown tag(s) compilation; known: complete")):
        vod_edits.vod_fields({"tags": ["compilation"]}, ("complete",))
    for value, message in (
        ("compilation", "list of strings"),
        ([1], "list of strings"),
        (["nope"], "unknown tag(s) nope; known: compilation"),
    ):
        with pytest.raises(ValueError, match=re.escape(message)):
            vod_edits.tags(value)


def test_content_end():
    assert vod_edits.content_end([{"start": 0, "end": 60}, {"start": 60, "end": 30.5}, {"start": "x"}, None]) == 90.5
    assert vod_edits.content_end(None) == 0


def test_chapters_stored_in_the_legacy_shape():
    out = vod_edits.chapters([ch(0, 3600), ch(3600, 1800.5, name="Artifact", restricted=True)], 5401)
    assert out == [
        {
            "gameId": "509658",
            "name": "Just Chatting",
            "image": "https://static-cdn.jtvnw.net/ttv-boxart/509658-40x53.jpg",
            "imageTemplate": TEMPLATE,
            "duration": "00:00:00",
            "start": 0,
            "end": 3600,
            "restricted": False,
        },
        {
            "gameId": "509658",
            "name": "Artifact",
            "image": "https://static-cdn.jtvnw.net/ttv-boxart/509658-40x53.jpg",
            "imageTemplate": TEMPLATE,
            "duration": "01:00:00",
            "start": 3600,
            "end": 1800.5,
            "restricted": True,
        },
    ]


def test_gap_chapters_stay_gap_chapters():
    gap = {
        "name": "Technical difficulties",
        "gameId": None,
        "start": 10,
        "length": 5,
        "restricted": True,
        "kind": "gap",
    }
    [plain, out] = vod_edits.chapters([ch(0, 10), gap], 15)
    assert out["kind"] == "gap" and "kind" not in plain


def test_chapter_without_category_or_image():
    [out] = vod_edits.chapters([{"name": None, "gameId": None, "start": 0, "length": 10, "restricted": False}], 10)
    assert (out["name"], out["gameId"], out["image"], out["imageTemplate"]) == (None, None, None, None)


@pytest.mark.parametrize(
    ("chapters", "duration", "message"),
    [
        ("nope", 100, "chapters must be a list"),
        ([ch(10, 5), ch(0, 5)], 100, "sort chapters by start"),
        ([ch(0, 50), ch(40, 10)], 100, "inside chapters[0]"),
        ([ch(0, 0)], 100, "length must be a number of seconds > 0"),
        ([ch(-1, 5)], 100, "start must be a number"),
        ([ch(90, 20)], 100, "after the end of the VOD"),
        ([ch(0, True)], 100, "length must be"),
        ([{**ch(0, 5), "restricted": "no"}], 100, "restricted must be true or false"),
        ([{**ch(0, 5), "gameId": 5}], 100, "gameId must be a string"),
        ([{**ch(0, 5), "extra": 1}], 100, "unknown field(s) extra"),
        ([ch(0, 5, kind="cut")], 100, "kind must be 'gap' or absent"),
        ([{"name": "x", "start": 0, "length": 5, "restricted": False}], 100, "missing gameId"),
    ],
)
def test_bad_chapters(chapters, duration, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        vod_edits.chapters(chapters, duration)


def test_chapter_bounds_tolerate_rounding():
    vod_edits.chapters([ch(0, 50.0004), ch(50, 50.5)], 100)  # float noise where chapters meet; lost fraction
    vod_edits.chapters([ch(0, 1000)], 0)  # duration unknown: not checked


def test_youtube_keeps_thumbnails_and_durations():
    existing = [{"id": "abc", "type": "vod", "duration": 3600, "part": 1, "thumbnail_url": "https://thumb/abc"}]
    out = vod_edits.youtube(
        [{"id": "abc", "type": "vod", "part": 1}, {"id": "def", "type": "live", "part": 1, "duration": 12.5}],
        existing,
    )
    assert out == [
        {"id": "abc", "type": "vod", "duration": 3600, "part": 1, "thumbnail_url": "https://thumb/abc"},
        {
            "id": "def",
            "type": "live",
            "duration": 12.5,
            "part": 1,
            "thumbnail_url": "https://i.ytimg.com/vi/def/mqdefault.jpg",
        },
    ]


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([{"id": "a", "type": "clip", "part": 1}], "type must be 'vod' or 'live'"),
        ([{"id": "", "type": "vod", "part": 1}], "id must be a non-empty string"),
        ([{"id": "a", "type": "vod", "part": 0}], "part must be a whole number"),
        ([{"id": "a", "type": "vod", "part": 1}, {"id": "a", "type": "live", "part": 1}], "listed twice"),
        ([{"id": "a", "type": "vod", "part": 1}, {"id": "b", "type": "vod", "part": 1}], "already a vod part 1"),
        ([{"id": "a", "type": "vod", "part": 1, "duration": -1}], "duration must be"),
    ],
)
def test_bad_youtube(items, message):
    with pytest.raises(ValueError, match=message):
        vod_edits.youtube(items, [])


def test_drive():
    assert vod_edits.drive([{"id": " x ", "type": "live"}]) == [{"id": "x", "type": "live"}]
    with pytest.raises(ValueError, match="listed twice"):
        vod_edits.drive([{"id": "x", "type": "vod"}, {"id": "x", "type": "live"}])
    with pytest.raises(ValueError, match="missing type"):
        vod_edits.drive([{"id": "x"}])
