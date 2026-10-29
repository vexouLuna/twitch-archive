"""Response schemas for the public API's OpenAPI document (/docs).

The routes return pre-rendered, cached JSON, so these are documentation only: nothing validates a response
against them. The wire shapes are the contract with the sites; change these to match, never the other way round.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FeathersPage(BaseModel):
    """A Feathers ``find``: one page of rows plus the paging totals."""

    total: int
    limit: int
    skip: int
    data: list[dict[str, Any]]


class FeathersError(BaseModel):
    name: str
    message: str
    code: int
    className: str


class LegacyError(BaseModel):
    error: bool = True
    msg: str


class CommentsPage(BaseModel):
    """Up to 200 chat messages. An empty ``comments`` list means no chat there, not an error."""

    comments: list[dict[str, Any]]
    cursor: str | None = Field(None, description="Pass as ?cursor= for the next page; absent on the last page.")
    sources: dict[str, int] | None = Field(
        None, description="Chat rows per source (replay, bot) for the VOD; only on offset pages."
    )
    segments: list[dict[str, Any]] | None = Field(None, description="Only on a synthetic VOD: its source windows.")


class Game(BaseModel):
    name: str | None
    gameId: str | None
    image: str | None
    imageTemplate: str | None


class GamePlayed(Game):
    vods: int
    chapters: int
    lastPlayed: str | None
    seconds: int
    watchableSeconds: int


class Status(BaseModel):
    live: bool
    stream: dict[str, Any] | None
    vod: dict[str, Any] | None


class ThirdPartyEmotes(BaseModel):
    """Emotes per provider (``7tv``, ``bttv``, ``ffz``), plus the providers that failed this time."""

    model_config = {"extra": "allow"}
    failed: list[str]


class SiteTags(BaseModel):
    tags: Any


class Badges(BaseModel):
    channel: Any
    global_: Any = Field(alias="global")


SiteOptions = dict[str, Any]
