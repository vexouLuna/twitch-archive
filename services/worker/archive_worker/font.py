"""How wide a line of text is in a TrueType font, read from the font's own tables (cmap format 12 and hmtx), so censor
text can be sized to fit its box before ffmpeg's drawtext draws it. Advance widths only: kerning, which drawtext
applies, only narrows a line. vods-core's preview reads the same font file the same way (fontMetrics.ts)."""

import struct
from functools import cache
from pathlib import Path


@cache
def _metrics(path: str) -> tuple[int, dict[int, int], tuple[int, ...]]:
    """(units per em, code point -> glyph, each glyph's advance) of the font at ``path``."""
    data = Path(path).read_bytes()
    tables = {}
    for i in range(struct.unpack_from(">H", data, 4)[0]):
        tag, _, offset, _ = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        tables[tag] = offset
    units = struct.unpack_from(">H", data, tables[b"head"] + 18)[0]
    hmtx, count = tables[b"hmtx"], struct.unpack_from(">H", data, tables[b"hhea"] + 34)[0]
    advances = tuple(struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(count))
    cmap = tables[b"cmap"]
    for i in range(struct.unpack_from(">H", data, cmap + 2)[0]):
        platform, encoding, offset = struct.unpack_from(">HHI", data, cmap + 4 + 8 * i)
        at = cmap + offset
        if (platform, encoding) in ((3, 10), (0, 4)) and struct.unpack_from(">H", data, at)[0] == 12:
            glyphs: dict[int, int] = {}
            for j in range(struct.unpack_from(">I", data, at + 12)[0]):
                first, last, glyph = struct.unpack_from(">III", data, at + 16 + 12 * j)
                glyphs.update((c, glyph + c - first) for c in range(first, last + 1))
            return units, glyphs, advances
    raise ValueError(f"{path}: no Unicode cmap (format 12) to measure text with")


def advance(path: str, text: str) -> tuple[int, int]:
    """(the advance widths of ``text``'s characters added up, units per em) in the font at ``path``; a character the
    font lacks counts as its missing-glyph box (glyph 0), as drawtext draws it."""
    units, glyphs, advances = _metrics(path)
    return sum(advances[min(glyphs.get(ord(c), 0), len(advances) - 1)] for c in text), units
