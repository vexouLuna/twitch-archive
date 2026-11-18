"""The short-chapter setting: a chapter under short_chapter_minutes is merged into a neighbour when chapters are
fetched (planning.merge_short_chapters). ``end`` of a chapter is its LENGTH in seconds, not an end time."""

import datetime as dt
import itertools
import random
from typing import Any

from archive_common.models import Vod
from archive_worker import planning
from archive_worker.steps import metadata

MIN = 300  # five minutes
RESTRICTED = ["Artifact"]


def ch(name: str | None, start: float, length: float, restricted: bool = False) -> dict[str, Any]:
    return planning.chapter(name and name.lower(), name, None, start, length, restricted)


def merge(chapters, into_next=True, minutes=MIN, restricted=RESTRICTED):
    return planning.merge_short_chapters(chapters, minutes, into_next, restricted)


def spans(chapters):
    return [(c["name"], c["start"], c["end"], c["restricted"]) for c in chapters]


def test_a_short_chapter_goes_to_the_following_one():
    out = merge([ch("A", 0, 3600), ch("B", 3600, 120), ch("C", 3720, 3000)])
    # C now starts where B did and its LENGTH grew by B's: its end time (start + end) is unchanged
    assert spans(out) == [("A", 0, 3600, False), ("C", 3600, 3120, False)]
    assert out[1]["duration"] == "01:00:00"
    assert out[1]["start"] + out[1]["end"] == 6720


def test_or_to_the_previous_one():
    out = merge([ch("A", 0, 3600), ch("B", 3600, 120), ch("C", 3720, 3000)], into_next=False)
    assert spans(out) == [("A", 0, 3720, False), ("C", 3720, 3000, False)]


def test_the_first_or_last_chapter_falls_back_to_the_other_side():
    assert spans(merge([ch("A", 0, 60), ch("B", 60, 3600)])) == [("B", 0, 3660, False)]
    assert spans(merge([ch("A", 0, 3600), ch("B", 3600, 60)])) == [("A", 0, 3660, False)]
    assert spans(merge([ch("A", 0, 60), ch("B", 60, 3600)], into_next=False)) == [("B", 0, 3660, False)]
    assert spans(merge([ch("A", 0, 3600), ch("B", 3600, 60)], into_next=False)) == [("A", 0, 3660, False)]


def test_a_single_chapter_is_left_alone():
    one = [ch("A", 0, 60)]
    assert merge(one) == one


def test_zero_minutes_turns_it_off_and_the_threshold_is_exact():
    chapters = [ch("A", 0, 3600), ch("B", 3600, 120), ch("C", 3720, 3000)]
    assert merge(chapters, minutes=0) == chapters
    exactly = [ch("A", 0, 3600), ch("B", 3600, 300), ch("C", 3900, 3000)]  # 5:00 is long enough
    assert merge(exactly) == exactly
    assert len(merge([ch("A", 0, 3600), ch("B", 3600, 299.9), ch("C", 3899.9, 3000)])) == 2


def test_chapters_in_a_row_keep_merging_until_long_enough():
    out = merge([ch("A", 0, 3600), ch("B", 3600, 60), ch("C", 3660, 60), ch("D", 3720, 60), ch("E", 3780, 3000)])
    assert spans(out) == [("A", 0, 3600, False), ("E", 3600, 3180, False)]
    out = merge([ch("A", 0, 3600), ch("B", 3600, 100), ch("C", 3700, 100), ch("D", 3800, 3000)], into_next=False)
    assert spans(out) == [("A", 0, 3800, False), ("D", 3800, 3000, False)]


def test_restricted_is_the_absorbing_games():
    # short into a restricted game: cut with it
    out = merge([ch("A", 0, 3600), ch("B", 3600, 60), ch("Artifact", 3660, 3000, True)])
    assert spans(out) == [("A", 0, 3600, False), ("Artifact", 3600, 3060, True)]
    # a short restricted chapter merged into an unrestricted game is uploaded with it
    out = merge([ch("A", 0, 3600), ch("Artifact", 3600, 60, True), ch("C", 3660, 3000)])
    assert spans(out) == [("A", 0, 3600, False), ("C", 3600, 3060, False)]
    out = merge([ch("Artifact", 0, 3600, True), ch("B", 3600, 60), ch("C", 3660, 3000)], into_next=False)
    assert spans(out) == [("Artifact", 0, 3660, True), ("C", 3660, 3000, False)]


def test_the_restricted_list_counts_not_only_the_stored_flag():
    # chapters stored before a game was added to restricted_games: the list decides for the merged span
    out = merge([ch("A", 0, 3600), ch("B", 3600, 60), ch("Artifact", 3660, 3000, False)])
    assert [c["restricted"] for c in out] == [False, True]


def test_a_short_no_category_gap_is_a_chapter_like_any_other():
    gap = [ch("A", 0, 3600), ch(None, 3600, 30), ch("C", 3630, 3000)]
    assert spans(merge(gap)) == [("A", 0, 3600, False), ("C", 3600, 3030, False)]
    # listed in restricted_games, a gap absorbed by an unrestricted game is uploaded with it
    cut = [ch("A", 0, 3600), ch(None, 3600, 30, True), ch("C", 3630, 3000)]
    assert spans(merge(cut, restricted=[planning.NO_CATEGORY])) == [("A", 0, 3600, False), ("C", 3600, 3030, False)]


def test_running_it_twice_changes_nothing_and_never_drops_time():
    rng = random.Random(7)
    for _ in range(300):
        t, chapters = 0.0, []
        for _i in range(rng.randint(1, 9)):
            length = rng.choice([30, 120, 299, 300, 900, 4000])
            name = rng.choice(["A", "B", None, "Artifact"])
            chapters.append(ch(name, t, length, planning.restricted_game(name, RESTRICTED)))
            t += length
        before = [dict(c) for c in chapters]
        for into_next in (True, False):
            once = merge(chapters, into_next)
            assert merge(once, into_next) == once
            assert once[0]["start"] == 0 and once[-1]["start"] + once[-1]["end"] == t  # still covers the VOD
            for a, b in itertools.pairwise(once):
                assert a["start"] + a["end"] == b["start"]  # no hole, no overlap
        assert chapters == before  # the input is not mutated


def test_chapters_from_moments_then_merge_end_is_a_length():
    def edge(pos, dur, name, gid):
        game = {"id": gid, "displayName": name, "boxArtURL": None}
        return {
            "node": {"positionMilliseconds": pos * 1000, "durationMilliseconds": dur * 1000, "details": {"game": game}}
        }

    edges = [edge(0, 3600, "A", "1"), edge(3600, 90, "B", "2"), edge(3690, 3000, "C", "3")]
    out = merge(planning.chapters_from_moments(edges, 6690, []))
    assert [(c["gameId"], c["start"], c["end"]) for c in out] == [("1", 0, 3600), ("3", 3600, 3090)]


# ── the chapters step and a live recording ──


async def test_the_step_stores_the_merged_chapters_and_off_restores_them(make_ctx, monkeypatch):
    created = dt.datetime(2026, 10, 8, tzinfo=dt.UTC)
    vod = Vod(id="100", title="t", created_at=created, duration="01:56:00", chapters_locked=False)
    moments = []
    for p, d, g in [(0, 3600, "A"), (3600, 120, "B"), (3720, 3240, "C")]:
        game = {"id": g, "displayName": g, "boxArtURL": None}
        moments.append(
            {"node": {"positionMilliseconds": p * 1000, "durationMilliseconds": d * 1000, "details": {"game": game}}}
        )
    saved: list[list[dict[str, Any]]] = []

    ctx = make_ctx("chapters", "100")

    async def get_vod():
        return vod

    async def update_vod(**values: Any) -> None:
        saved.append(values["chapters"])

    async def video_moments(_id):
        return moments

    async def refuse():
        return None

    monkeypatch.setattr(ctx, "get_vod", get_vod)
    monkeypatch.setattr(ctx, "update_vod", update_vod)
    monkeypatch.setattr(ctx, "refuse_if_spliced", refuse)
    monkeypatch.setattr(ctx.deps.gql, "video_moments", video_moments)

    def last():
        return [(c["name"], c["start"], c["end"]) for c in saved[-1]]

    assert ctx.settings.short_chapter_minutes == 5 and ctx.settings.short_chapter_merge == "following"
    await metadata.chapters(ctx)
    assert last() == [("A", 0, 3600), ("C", 3600, 3360)]
    ctx.settings.short_chapter_minutes = 0  # off, and the job run again: Twitch's chapters are back
    await metadata.chapters(ctx)
    assert last() == [("A", 0, 3600), ("B", 3600, 120), ("C", 3720, 3240)]
    ctx.settings.short_chapter_minutes = 5
    ctx.settings.short_chapter_merge = "previous"
    await metadata.chapters(ctx)
    assert last() == [("A", 0, 3720), ("C", 3720, 3240)]

    n = len(saved)
    vod.chapters_locked = True  # hand-edited chapters are never touched, nor refetched
    await metadata.chapters(ctx)
    assert len(saved) == n


def test_a_live_recording_keeps_its_edge_chapters_so_gaps_stay_where_they_were(make_ctx):
    """part_gaps works from the VOD's chapters, so the recording's chapters must cut the same stretches: a short
    edge chapter next to a restricted one is not merged into it here (the review's example)."""
    start = dt.datetime(2026, 10, 8, 12, tzinfo=dt.UTC)
    chapters = [ch("A", 0, 3600), ch("Artifact", 3600, 3600, True), ch("C", 7200, 3600)]
    vod = Vod(id="100", title="t", created_at=start, duration="03:00:00", chapters=chapters)
    t0 = start.timestamp()
    # recorded from VOD 3540, with a hole at VOD 9000-9060; the file is 7200 s
    timeline = [[0.0, t0 + 3540, 5460.0], [5460.0, t0 + 9060, 1740.0]]
    ctx = make_ctx("live", "100", {"type": "live", "timeline": timeline, "duration": 7200.0})
    file = metadata.file_chapters(ctx, vod)
    assert file is not None and [(c["name"], c["start"], c["end"]) for c in file] == [
        ("A", 0, 60),
        ("Artifact", 60, 3600),
        ("C", 3660, 3540),
    ]
    parts = planning.plan_parts(7200.0, file, RESTRICTED, 10800)
    assert [(p.start, p.end) for p in parts] == [(0, 60), (3660, 7200)]
    gaps = metadata.part_gaps(ctx, vod, parts)
    assert gaps == {2: [[1800, 60]]}  # 1800 s into the part that starts at C, not 1860
