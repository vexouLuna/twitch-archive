"""archive-worker entrypoint.

archive-worker run [--dry-run]            monitor + jobs + admin API (just /healthz, /readyz with ARCHIVE_ADMIN_APART)
archive-worker admin                      the admin API on its own (ARCHIVE_ADMIN_APART; one instance only)
archive-worker import-youtube-token FILE  import youtube.auth from a legacy config.json
archive-worker enqueue KIND VOD_ID [JSON] queue a job without the admin API
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path

import uvicorn
from archive_common import http, logs
from archive_common.config import get_settings
from archive_common.twitch.gql import Gql
from archive_common.twitch.helix import Helix
from fastapi import FastAPI
from vex_platform.actor import Actor

from . import admin_status, jobs, youtube
from .admin import create_admin_app
from .admin_auth import DbSessionStore
from .context import Deps
from .events import JobEvents
from .monitor import Monitor
from .runtime_settings import RuntimeSettings

log = logging.getLogger("archive_worker")

SETTINGS_RELOAD_S = 10  # with the admin apart, how soon the worker sees a dashboard setting change


def _server(app: FastAPI, settings) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.admin_host,
            port=settings.admin_port,
            log_level=settings.log_level.lower(),
            log_config=None,  # use the handler from logs.setup
            proxy_headers=False,
        )
    )
    server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]  # we handle signals ourselves
    return server


def _stop_on_signal() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows dev boxes
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    return stop


async def _reload_settings(runtime: RuntimeSettings, service: jobs.JobService) -> None:
    """The admin process writes the dashboard's settings; this one picks them up from the database."""
    while True:
        await asyncio.sleep(SETTINGS_RELOAD_S)
        try:
            before = {k: row.value for k, row in runtime.overrides.items()}
            await runtime.load()
            service.apply_settings()
            after = {k: row.value for k, row in runtime.overrides.items()}
            if changed := sorted(k for k in before.keys() | after.keys() if before.get(k, ...) != after.get(k, ...)):
                log.info("runtime settings changed: %s", ", ".join(changed))
        except Exception:
            log.exception("could not reload the runtime settings")


async def serve(dry_run: bool = False) -> None:
    settings = get_settings()
    if dry_run:
        settings.dry_run = True
    runtime = RuntimeSettings(settings)
    await runtime.load()  # the dashboard's overrides, over the env values
    events = JobEvents()
    events.install()  # job log lines -> GET /admin/jobs/{id}/events
    deps = Deps(settings, Helix(settings), Gql(settings), youtube.YouTube(settings), events)
    service = jobs.JobService.create(deps)
    monitor = Monitor(deps.helix, service, settings)
    if settings.admin_apart:
        app = FastAPI(title="archive-worker", docs_url=None, redoc_url=None, openapi_url=None)
        admin_status.register_health(app, service)
    else:
        app = create_admin_app(deps, service, sessions=DbSessionStore(), runtime=runtime)
    admin = _server(app, settings)
    stop = _stop_on_signal()

    log.info(
        "worker starting: channel=%s live_record=%s vod_download=%s youtube_upload=%s dry_run=%s admin_apart=%s",
        settings.twitch_username,
        settings.live_record,
        settings.vod_download,
        settings.youtube_upload,
        settings.dry_run,
        settings.admin_apart,
    )
    await service.runtime.open()
    await service.runtime.start()
    tasks = [
        asyncio.create_task(monitor.run_forever(), name="monitor"),
        asyncio.create_task(events.run_forever(), name="job-events"),  # flushes on cancel
    ]
    if settings.google_client_id:
        # Daily refresh (while uploads are on): surfaces a revoked token in the logs before a
        # stream needs it, and keeps Google from revoking it after six months without uploads.
        tasks.append(asyncio.create_task(deps.youtube.keepalive(), name="youtube-keepalive"))

        async def connected() -> bool:
            return bool((await deps.youtube.cached_check())["valid"])

        tasks.append(asyncio.create_task(deps.youtube_check.run_forever(connected), name="youtube-check"))
    if settings.admin_apart:
        tasks.append(asyncio.create_task(_reload_settings(runtime, service), name="settings-reload"))
    tasks.append(asyncio.create_task(admin.serve(), name="admin"))  # keep last: shutdown awaits it
    stopper = asyncio.create_task(stop.wait())
    pending: set[asyncio.Task] = {*tasks, stopper}  # type: ignore[type-arg]
    while True:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        crashed = [t for t in done if t is not stopper and t.exception()]
        for t in crashed:
            log.error("%s crashed", t.get_name(), exc_info=t.exception())
        # The monitor returns normally when Twitch credentials are missing; keep serving.
        if stopper in done or crashed or any(t.get_name() != "monitor" for t in done):
            break

    log.info("shutting down; interrupted jobs resume on next start")
    admin.should_exit = True  # uvicorn stops on its own; cancelling it logs a spurious traceback
    await service.runtime.stop()
    admin_task, others = tasks[-1], tasks[:-1]
    for t in others:
        t.cancel()
    await asyncio.gather(*others, return_exceptions=True)
    try:
        await asyncio.wait_for(admin_task, timeout=10)
    except (TimeoutError, asyncio.CancelledError, Exception):
        pass
    await service.runtime.close()
    await http.close_client()


async def serve_admin() -> None:
    """The admin API without the capture loop: it queues and acts on jobs through the database, and the
    worker (ARCHIVE_ADMIN_APART) runs them. Sign-in states and the login limiter stay in memory, so run one."""
    settings = get_settings()
    runtime = RuntimeSettings(settings)
    await runtime.load()
    deps = Deps(settings, Helix(settings), Gql(settings), youtube.YouTube(settings), JobEvents())
    service = jobs.JobService.create(deps)
    admin = _server(create_admin_app(deps, service, sessions=DbSessionStore(), runtime=runtime), settings)
    stop = _stop_on_signal()

    log.info("admin API starting apart from the worker")
    await service.runtime.open()  # the pool, for enqueueing; the runtime's worker is not started here
    notes = asyncio.create_task(service.runtime.events.run_forever(), name="job-notes")  # lines admin actions add
    server, stopper = asyncio.create_task(admin.serve(), name="admin"), asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({server, notes, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t is not stopper and t.exception():
            log.error("%s crashed", t.get_name(), exc_info=t.exception())

    admin.should_exit = True
    try:
        await asyncio.wait_for(server, timeout=10)
    except (TimeoutError, asyncio.CancelledError, Exception):
        pass
    for t in (notes, stopper):
        t.cancel()
    await asyncio.gather(notes, stopper, return_exceptions=True)
    await service.runtime.close()  # flushes the notes
    await http.close_client()


async def _enqueue(kind: str, vod_id: str, payload: str | None) -> None:
    settings = get_settings()
    service = jobs.JobService.create(Deps(settings, Helix(settings), Gql(settings), youtube.YouTube(settings)))
    await service.runtime.open()
    try:
        job = await service.enqueue(
            kind, vod_id, json.loads(payload) if payload else {}, actor=Actor("system", via="cli")
        )
    finally:
        await service.runtime.close()
    print(f"queued job {job.id} ({kind} {vod_id}, {job.state}); a running worker picks it up at once")


def run() -> None:
    parser = argparse.ArgumentParser(prog="archive-worker")
    sub = parser.add_subparsers(dest="cmd")
    p_run = sub.add_parser("run", help="run monitor, job runner and admin API (default)")
    p_run.add_argument("--dry-run", action="store_true", help="download and process, but never upload or delete")
    sub.add_parser("admin", help="run the admin API on its own (with ARCHIVE_ADMIN_APART on the worker)")
    p_tok = sub.add_parser("import-youtube-token", help="import youtube.auth from the legacy config.json")
    p_tok.add_argument("config", type=Path)
    p_enq = sub.add_parser("enqueue", help="queue a job directly in the database")
    p_enq.add_argument("kind", choices=sorted(jobs.KINDS))
    p_enq.add_argument("vod_id")
    p_enq.add_argument("payload", nargs="?", help="JSON object, e.g. '{\"start_part\": 2}'")
    args = parser.parse_args()

    logs.setup(get_settings().log_level, get_settings().log_format)
    if args.cmd == "import-youtube-token":
        asyncio.run(youtube.import_legacy_token(args.config))
        print("YouTube refresh token imported.")
    elif args.cmd == "admin":
        asyncio.run(serve_admin())
    elif args.cmd == "enqueue":
        asyncio.run(_enqueue(args.kind, args.vod_id, args.payload))
    else:
        asyncio.run(serve(dry_run=getattr(args, "dry_run", False)))


if __name__ == "__main__":
    run()
