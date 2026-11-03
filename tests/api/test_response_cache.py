"""ResponseCache must not keep a render that an invalidation overtook."""

import asyncio

from archive_api.middleware import ResponseCache


async def test_a_render_overtaken_by_an_invalidation_is_not_cached():
    cache = ResponseCache(300)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def factory():
        calls.append(1)
        data = f"v{len(calls)}"  # read before the write commits
        started.set()
        await release.wait()
        return data

    first = asyncio.create_task(cache.get_or_render("vods/1", factory))
    await started.wait()
    cache.invalidate(lambda key: key == "vods/1")  # the admin write's NOTIFY lands mid-render
    release.set()
    await first
    assert cache.get("vods/1") is None  # the pre-write body was not kept
    second = await cache.get_or_render("vods/1", factory)  # a miss renders afresh, not joining the old one
    assert len(calls) == 2 and second.raw == b'"v2"'
