"""The site's timeline model (pure functions): where a live recording's uploads land on its VOD."""

from dataclasses import dataclass
from typing import Any

from archive_worker import planning, timeline
from archive_worker.vod_edits import GAP_KIND


@dataclass(frozen=True)
class Vod:
    id: str
    duration: float
    chapters: list[Any]
    youtube: list[Any]


def ch(start, length, name="Just Chatting", restricted=False):
    return planning.chapter("509658", name, None, start, length, restricted)


def live(vid, part, duration, gaps):
    return {"id": vid, "type": "live", "duration": duration, "part": part, "gaps": gaps}


# 4000 s VOD: recording starts 50 s in, misses 150 s at VOD 1850 and 100 s at VOD 3600 (part 2's end).
GAPPED = Vod("g", 4000, [ch(0, 4000)], [live("g1", 1, 1950, [[1800, 150]]), live("g2", 2, 1750, [[1750, 100]])])


def test_gaps_leave_only_the_missing_start_as_delay():
    t = timeline.Timeline.of("g", GAPPED.duration, GAPPED.chapters, GAPPED.youtube, "live")
    assert t.delay == 50
    assert t.holes == [(1850, 2000), (3900, 4000)]
    assert t.vod_time(0) == 50
    assert t.vod_time(1800) == 2000  # right after the ad break
    assert t.vod_time(1800, ending=True) == 1850
    assert t.vod_time(2000) == 2200  # part 2 starts 150 s later than without gaps
    assert t.part_spans() == [(50, 2150), (2150, 4000)]


def test_gaps_and_cuts_together():
    vod = Vod("g", 2200, [ch(0, 1000), ch(1000, 100, restricted=True), ch(1100, 1100)], GAPPED.youtube[:1])
    t = timeline.Timeline.of("g", vod.duration, vod.chapters, vod.youtube, "live")
    assert t.delay == 0  # 2200 − 1950 − 150 − 100
    assert t.holes == [(1000, 1100), (1900, 2050)]
    assert t.vod_time(1800) == 2050


def test_chapters_clip_and_shift_for_synthetic_vods():
    chapters = [ch(0, 100), ch(100, 200, "Minecraft")]
    assert [(c["name"], c["start"], c["end"]) for c in timeline.clip(chapters, 50, 150)] == [
        ("Just Chatting", 50, 50),
        ("Minecraft", 100, 50),
    ]
    assert [c["start"] for c in timeline.shift(chapters, 10)] == [10, 110]
    gap = timeline.gap_chapter(300, 20)
    assert (gap["restricted"], gap["kind"], gap["start"], gap["end"]) == (True, GAP_KIND, 300, 20)
