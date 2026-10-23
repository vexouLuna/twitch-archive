"""Turn each active merge (``vod_splices``) into the equivalent synthetic VOD (ADR 0004, phase 2).

``archive-worker convert-splices`` reports what it would do; ``--apply`` does it. For each active merge
of B into A at ``offset_s``, in one transaction:

1. the merge is undone (``splices._undo``, forced: the checks below stand in for its edit check), so
   A and B are again the real VODs they were;
2. the upload keys added since (``CARRY``, matched by YouTube video id) are put back on them;
3. hand-made synthetic VODs that play A on the merged timeline get segments on A and B instead (``auto``
   ones follow their game on the monitor's next round);
4. a synthetic VOD ``A+B`` that supersedes both is made, titled and tagged as A was, and audited
   (``splice.convert``, ``synthetic.create``).

A dry run does all of it and rolls it back, so it checks what ``--apply`` would hit. A merge is reported
and left alone when it can't be converted that way: a split or a chain (none existed when this was
written: undo those by hand), an active job, censor ranges, a hidden VOD, or edits since the merge other
than ``CARRY``. Safe to run again: a converted merge is undone, so it isn't picked up.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from archive_common import audit
from archive_common.db import get_sessionmaker
from archive_common.models import Vod, VodSegment, VodSplice
from archive_common.segments import EPS, Segment
from archive_common.timeutil import hhmmss_to_seconds
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from vex_platform.actor import SYSTEM
from vex_platform.audit import AuditEntry
from vex_platform.audit.sqlalchemy import record

from . import compose, splices, synthetic
from .job_rows import ACTIVE, ALL_JOBS

CARRY = ("preview", "gaps")  # per-upload keys later jobs add (seek-bar previews, live gaps)
EDITABLE = splices.EDITABLE


class Refused(Exception):
    pass


@dataclass
class Report:
    splice: int
    target: str
    source: str
    synthetic: str | None = None
    refused: str | None = None
    carried: int = 0  # upload entries that got their CARRY keys back
    repointed: list[str] = field(default_factory=list)

    def line(self) -> str:
        head = f"splice {self.splice}: {self.source} merged into {self.target}"
        if self.refused:
            return f"{head}: left alone: {self.refused}"
        extra = f"; repointed {', '.join(self.repointed)}" if self.repointed else ""
        return f"{head} -> {self.synthetic} (upload keys carried over on {self.carried} part(s){extra})"


def _beyond_carry(now: list[Any], then: list[Any]) -> str | None:
    """How ``now`` differs from ``then`` beyond ``CARRY`` keys on the same uploads, or None."""
    if [e.get("id") for e in now] != [e.get("id") for e in then]:
        return "uploads added, removed or reordered"
    for a, b in zip(now, then, strict=True):
        keys = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)} - set(CARRY)
        if keys:
            return f"upload {a.get('id')} changed: {', '.join(sorted(keys))}"
    return None


def _edits(sp: VodSplice, vods: dict[str, Vod]) -> list[str]:
    """What was changed on the VODs since the merge, other than ``CARRY`` keys of their uploads."""
    out = []
    for vod_id, fields in sp.snapshot["result"].items():
        vod = vods[vod_id]
        for k in EDITABLE:
            now, then = getattr(vod, k), fields.get(k)
            if now == then:
                continue
            why = _beyond_carry(now or [], then or []) if k == "youtube" else "changed"
            if why:
                out.append(f"{vod_id}.{k} ({why})")
    return out


def remap(seg: Segment, a: str, b: str, a_duration: float, offset: float, merged_end: float) -> list[Segment]:
    """A segment on merged A's timeline, as segments on A (up to its own end) and B (from ``offset``)."""
    if seg.source_id != a:
        return [seg]
    end = merged_end if seg.end is None else seg.end
    out = []
    if seg.start < a_duration - EPS:
        out.append(Segment(a, seg.start, min(end, a_duration), seg.at, seg.label))
    if end > offset + EPS:
        start = max(seg.start, offset)
        out.append(
            Segment(b, start - offset, None if seg.end is None else end - offset, seg.at + start - seg.start, seg.label)
        )
    return out


async def _hand_made_users(s: AsyncSession, vod_id: str) -> dict[str, list[Segment]]:
    """Synthetic VODs not following a game (``auto``) with a segment on ``vod_id``, and their segments."""
    ids = (
        await s.execute(
            select(VodSegment.vod_id)
            .join(Vod, Vod.id == VodSegment.vod_id)
            .where(VodSegment.source_id == vod_id, ~Vod.synthetic.has_key("auto"))
            .distinct()
            .order_by(VodSegment.vod_id)
        )
    ).scalars()
    return {i: await synthetic._segments(s, i) for i in ids}


async def _convert(s: AsyncSession, sp: VodSplice, report: Report) -> None:
    a, b = await splices._lock(s, sp.vod_id, sp.other_id)
    active = (
        await s.execute(select(ALL_JOBS.c.id).where(ALL_JOBS.c.vod_id.in_([a.id, b.id]), ALL_JOBS.c.state.in_(ACTIVE)))
    ).all()
    if active:
        raise Refused(f"jobs are active on them ({', '.join(str(j.id) for j in active)})")
    if a.censor or b.censor:
        raise Refused("censor ranges are set (on the merged timeline); remove them first")
    if a.hidden or b.hidden:
        raise Refused("a hidden VOD: decide by hand whether its synthetic VOD should be shown")
    if edits := _edits(sp, {a.id: a, b.id: b}):
        raise Refused("edited since the merge: " + "; ".join(edits))

    merged = {e.get("id"): e for vod in (a, b) for e in vod.youtube or [] if isinstance(e, dict)}
    title, tags = a.title, list(a.tags or [])
    merged_end = float(hhmmss_to_seconds(a.duration))
    a_duration = float(hhmmss_to_seconds(sp.snapshot["target"]["duration"]))
    offset = float(sp.offset_s)
    users = await _hand_made_users(s, a.id)

    await splices._undo(s, sp, force=True)
    for vod in (a, b):
        entries = []
        for e in vod.youtube or []:
            kept = {k: merged[e.get("id")][k] for k in CARRY if k in merged.get(e.get("id"), {})}
            report.carried += bool(kept and any(e.get(k) != v for k, v in kept.items()))
            entries.append({**e, **kept})
        vod.youtube = entries
    await s.flush()

    for user, segments in users.items():
        moved = [x for seg in segments for x in remap(seg, a.id, b.id, a_duration, offset, merged_end)]
        await synthetic.resegment_in(s, user, moved)
        report.repointed.append(user)

    new_id = compose.merge_id(a.id, b.id)
    segments = [
        Segment(a.id, 0.0, None if offset >= a_duration else offset, 0.0),
        Segment(b.id, 0.0, None, offset),
    ]
    view = await synthetic.create_in(s, new_id, segments, title=title, supersedes=True, tags=tags)
    report.synthetic = new_id
    detail = {"splice": sp.id, "merge": [a.id, b.id], "repointed": report.repointed, "via": "convert-splices"}
    await record(
        s, AuditEntry("synthetic.create", SYSTEM, f"vod:{new_id}", after=view, detail=detail), table=audit.TABLE
    )
    await record(
        s,
        AuditEntry("splice.convert", SYSTEM, f"vod:{a.id}", detail={**detail, "synthetic": new_id}),
        table=audit.TABLE,
    )


async def run(*, apply: bool = False) -> list[Report]:
    """Every active splice, oldest first: converted (``apply``) or tried and rolled back."""
    async with get_sessionmaker()() as s:
        active = list(
            (await s.execute(select(VodSplice).where(VodSplice.undone_at.is_(None)).order_by(VodSplice.id))).scalars()
        )
    uses = Counter(i for sp in active for i in (sp.vod_id, sp.other_id))
    reports = []
    for found in active:
        report = Report(found.id, found.vod_id, found.other_id)
        reports.append(report)
        if found.kind != "merge":
            report.refused = "a split: undo it by hand, then split again with /api/v2"
            continue
        if uses[found.vod_id] > 1 or uses[found.other_id] > 1:
            report.refused = "part of a chain of splices: undo them by hand, newest first"
            continue
        async with get_sessionmaker()() as s:
            await s.begin()
            try:
                sp = await s.get(VodSplice, found.id, with_for_update=True)
                if sp is None or sp.undone_at is not None:
                    report.refused = "undone meanwhile"
                    continue
                await _convert(s, sp, report)
            except (Refused, splices.SpliceError, synthetic.SyntheticError) as exc:
                await s.rollback()
                report.refused = getattr(exc, "msg", None) or str(exc)
                report.synthetic, report.carried, report.repointed = None, 0, []
                continue
            if apply:
                await s.commit()
            else:
                await s.rollback()
    return reports
