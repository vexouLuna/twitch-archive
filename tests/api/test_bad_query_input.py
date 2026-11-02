"""Malformed query-string and cursor input is a 400 or a miss, never a 500; none of it needs a database."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from archive_api import feathers_query as fq
from archive_api.comments import Comments
from archive_api.errors import FeathersError
from archive_api.middleware import ResponseCache
from archive_api.services import VodsService, build_services


def _parse(qs: str) -> Any:
    service = build_services(SimpleNamespace())  # type: ignore[arg-type]
    assert isinstance(service["vods"], VodsService)
    return fq.parse(service["vods"].resource, qs, default_limit=10, max_limit=10, special=service["vods"].special)


@pytest.mark.parametrize(
    "qs",
    ["$select[0][x]=y", "$select[a][b][c]=d", "tags[$nin]=vods", "tags[$ne]=vods", "tags[$exists]=true"],
)
def test_malformed_select_and_tags_operators_are_400(qs: str) -> None:
    with pytest.raises(FeathersError) as err:
        _parse(qs)
    assert err.value.code == 400


@pytest.mark.parametrize("qs", ["tags=vods", "tags[]=a&tags[]=b", "tags[$all][]=a", "tags[$in][]=a"])
def test_the_tag_forms_still_parse(qs: str) -> None:
    _parse(qs)


class _Column:
    """Stands in for a column: any comparison is fine, any attribute is another column."""

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return True

    def __ge__(self, other: object) -> bool:
        return True

    def __getattr__(self, name: str) -> _Column:
        return self


@pytest.mark.parametrize("offset", ["Infinity", "-Infinity", "NaN"])
async def test_a_cursor_with_a_non_finite_offset_is_a_miss(offset: str) -> None:
    comments = Comments(ResponseCache(300), ResponseCache(300))
    src: Any = SimpleNamespace(table=SimpleNamespace(c=_Column()), seq=_Column(), sent=_Column())
    cursor = {"id": 1, "createdAt": "2020-01-01T00:00:00.000Z", "content_offset_seconds": float(offset)}
    assert await comments._cursor_search(None, src, "1", cursor) is None  # type: ignore[arg-type]
