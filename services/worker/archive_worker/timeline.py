"""The site's VOD timeline (vods-core ``src/timeline.ts``): chapters, and uploads laid over a VOD.

Pure functions, no I/O. The site reads a VOD like this:

* chapters are in VOD seconds: ``start``, and ``end``, which holds the chapter's *length*;
* a ``restricted: true`` chapter is a cut: it is in no upload, and the player skips it;
* the uploads of one type (``vod`` or ``live``) play back to back in ``part`` order, and
  none of them contains a cut;
* an upload's ``gaps`` ([seconds into the upload, length] each) are VOD time that type's
  uploads miss there (a live recording's ad breaks); unlike a cut, they hold for that type only,
  and they travel with their upload;
* ``delay = duration − Σ part durations − Σ gap lengths − Σ cut lengths`` is footage missing
  at the start of the VOD: upload time 0 is VOD time ``delay`` (after any cuts it has to skip).

``compose`` builds synthetic VODs' chapters with the chapter helpers; ``Timeline`` is the site's model
of where an upload's frames land.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from archive_common.timeutil import format_hhmmss

from .planning import chapter, num_seconds
from .vod_edits import GAP_KIND, is_number

GAP_NAME = "Technical difficulties"  # the name of every gap chapter
EPS = 0.001  # float noise tolerated where two times should meet


class PlanError(ValueError):
    """A VOD whose uploads can't be laid over its timeline (a part without a duration)."""

    def __init__(self, msg: str, **extra: Any) -> None:
        super().__init__(msg)
        self.extra = extra


# ── Chapters ──────────────────────────────────────────────────────────────


def start_of(ch: dict[str, Any]) -> float:
    return float(ch.get("start") or 0)


def length_of(ch: dict[str, Any]) -> float:
    return float(ch.get("end") or 0)  # "end" is the length, not the end time


def gap_chapter(start: float, length: float) -> dict[str, Any]:
    return {**chapter(None, GAP_NAME, None, start, length, True), "kind": GAP_KIND}


def _chapters(chapters: Any) -> list[dict[str, Any]]:
    return [c for c in chapters or [] if isinstance(c, dict)]


def cuts(chapters: Any) -> list[tuple[float, float]]:
    """(start, end) of every restricted chapter, sorted."""
    return sorted(
        (start_of(c), start_of(c) + length_of(c))
        for c in _chapters(chapters)
        if c.get("restricted") is True and length_of(c) > 0
    )


def _moved(ch: dict[str, Any], start: float, length: float) -> dict[str, Any]:
    return {**ch, "duration": format_hhmmss(start), "start": num_seconds(start), "end": num_seconds(length)}


def clip(chapters: Any, lo: float, hi: float) -> list[dict[str, Any]]:
    """The part of each chapter inside [lo, hi); chapters left shorter than EPS are dropped.
    A chapter entirely inside is returned as it is."""
    out = []
    for ch in _chapters(chapters):
        s, e = start_of(ch), start_of(ch) + length_of(ch)
        if s >= lo and e <= hi:
            out.append(ch)
        elif min(e, hi) - max(s, lo) > EPS:
            out.append(_moved(ch, max(s, lo), min(e, hi) - max(s, lo)))
    return out


def shift(chapters: Any, by: float) -> list[dict[str, Any]]:
    return [_moved(ch, start_of(ch) + by, length_of(ch)) for ch in _chapters(chapters)]


# ── Uploads ───────────────────────────────────────────────────────────────


def parts(youtube: Any, typ: str) -> list[dict[str, Any]]:
    return sorted(
        (e for e in youtube or [] if isinstance(e, dict) and e.get("type") == typ), key=lambda e: e.get("part") or 0
    )


def upload_gaps(entry: dict[str, Any]) -> list[tuple[float, float]]:
    """An upload's ``gaps`` as (seconds into it, length); malformed ones are ignored."""
    return [
        (float(g[0]), float(g[1]))
        for g in entry.get("gaps") or []
        if isinstance(g, list) and len(g) == 2 and is_number(g[0]) and is_number(g[1])
    ]


# ── Timeline ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Timeline:
    """One type's uploads laid over a VOD's timeline."""

    duration: float
    parts: tuple[float, ...]  # durations, in play order
    cuts: tuple[tuple[float, float], ...]
    gaps: tuple[tuple[float, float], ...] = ()  # (upload time, length), sorted; see the module docstring

    @classmethod
    def of(cls, vod_id: str, duration: float, chapters: Any, youtube: Any, typ: str) -> Timeline:
        durations: list[float] = []
        gaps = []
        for e in parts(youtube, typ):
            if not is_number(e.get("duration")) or e["duration"] < 0:
                raise PlanError(
                    f"{vod_id}: {typ} part {e.get('part')} ({e.get('id')}) has no duration, so its "
                    "place on the timeline is unknown; set it with PUT /admin/vods/{id}/youtube"
                )
            gaps += [(sum(durations) + float(at), float(n)) for at, n in upload_gaps(e) if n > 0]
            durations.append(float(e["duration"]))
        return cls(float(duration), tuple(durations), tuple(cuts(chapters)), tuple(sorted(gaps)))

    @property
    def delay(self) -> float:
        return self.duration - sum(self.parts) - sum(n for _, n in self.gaps) - sum(e - s for s, e in self.cuts)

    @property
    def holes(self) -> list[tuple[float, float]]:
        """(start, end) in VOD time of every cut and gap, sorted: what these uploads skip."""
        out, cut_list, shift, i = list(self.cuts), list(self.cuts), self.delay, 0
        for at, n in self.gaps:
            v = at + shift
            while i < len(cut_list) and cut_list[i][0] <= v + EPS:  # a cut at the gap comes first
                v += cut_list[i][1] - cut_list[i][0]
                shift += cut_list[i][1] - cut_list[i][0]
                i += 1
            out.append((v, v + n))
            shift += n
        return sorted(out)

    def vod_time(self, upload_time: float, *, ending: bool = False) -> float:
        """Where ``upload_time`` lands on the VOD. A frame that starts right at a cut (or gap)
        lands after it; with ``ending``, the time is where the frame before it ends (before it)."""
        v = upload_time + self.delay
        for s, e in self.holes:
            if s < v - EPS or (not ending and s <= v + EPS):
                v += e - s
            else:
                break
        return v

    def part_spans(self) -> list[tuple[float, float]]:
        """(first frame, end of last frame) of each part, in VOD time. A gap at the end of a part
        is the part's: its span runs to the end of the gap."""
        spans, u = [], 0.0
        for d in self.parts:
            end = self.vod_time(u + d, ending=True)
            end += sum(n for at, n in self.gaps if u < at and abs(at - (u + d)) <= EPS)
            spans.append((self.vod_time(u), end))
            u += d
        return spans


# ── Emotes ────────────────────────────────────────────────────────────────

EMOTE_SETS = ("ffz_emotes", "bttv_emotes", "seventv_emotes")  # the emotes row's per-channel columns
