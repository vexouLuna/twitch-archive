"""Admin HTTP API (LAN only, port 3031).

Request bodies follow the legacy admin routes (``vodId``, ``type``, ...);
``platform`` is accepted and ignored (Twitch only). Every long-running action
enqueues a job and answers ``{"error": false, "msg": ..., "jobId": ...}``.

Every /admin route takes either ``Authorization: Bearer <admin_api_key>``
(scripts) or the dashboard's session cookie (see admin_auth), which comes from
the password (local network only) or a Twitch sign-in (see admin_signin). Every
state-changing request that succeeds is written to the audit log (``audit_log``, named as in
archive_common/audit.py): its body, or what the route put in ``request.state.audit_detail`` (e.g. a
VOD edit's before and after). Actions on jobs are audited by the job runtime (and ``JobService`` for
the legacy table's) instead, as every other way of acting on a job is.

This module builds the app: errors, auth and sessions, sign-in, the audit and CORS middleware, and
the helpers the routes share (``AdminContext``, admin_shared.py). The routes are in admin_status,
admin_site, admin_jobs, admin_vods, admin_twitch, admin_pipelines and admin_youtube.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import secrets
from typing import Any
from urllib.parse import urlencode

from archive_common.audit import actor_of, route_entry
from archive_common.audit import write as write_audit
from archive_common.db import get_sessionmaker
from archive_common.models import Vod
from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.base import RequestResponseEndpoint
from vex_platform.api import ApiError, problem

from . import (
    admin_jobs,
    admin_pipelines,
    admin_site,
    admin_status,
    admin_twitch,
    admin_vods,
    admin_youtube,
    api_v2,
    api_v2_routes,
    jobs,
    splices,
    vod_edits,
)
from .admin_auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AdminAuth,
    LoginLimiter,
    Session,
    SessionStore,
    client_address,
    forwarded_untrusted,
    parse_networks,
    parse_password_networks,
    password_allowed,
    plain_http,
)
from .admin_shared import (
    AUDITED_PREFIXES,
    CORS_HEADERS,
    JOB_ROUTES,
    LAN_SESSION_COOKIE_ARGS,
    SAFE_METHODS,
    SESSION_COOKIE_ARGS,
    SESSION_PATH,
    STATE_COOKIE_ARGS,
    TWITCH_STEPS,
    YOUTUBE_CHECK_MAX_AGE,
    AdminContext,
    AdminError,
    Unauthenticated,
    _actor,
    _ok,
    log,
    login_of,
    origin_of,
)
from .admin_signin import (
    CHECK_S,
    ERRORS,
    STATE_COOKIE,
    STATE_TTL_S,
    AuthClient,
    PendingStates,
    SignInError,
    VexAuth,
    safe_next,
)
from .context import Deps
from .events import iso_utc
from .runtime_settings import RuntimeSettings
from .storage import Storage, StorageError
from .vods import splice_reason


def create_admin_app(
    deps: Deps,
    service: jobs.JobService,
    signin: AuthClient | None = None,
    sessions: SessionStore | None = None,
    runtime: RuntimeSettings | None = None,
) -> FastAPI:
    """``signin``: the vex-auth client; by default built from the settings (None when not configured).
    ``sessions``: where dashboard sessions are kept; the worker passes the database's, tests leave memory.
    ``runtime``: the dashboard's setting overrides, already loaded; by default a fresh one over ``deps.settings``."""
    settings = deps.settings
    runtime = runtime or RuntimeSettings(settings)
    storage = Storage(settings.data_dir)
    helix = deps.helix
    app = FastAPI(title="archive-worker admin", docs_url=None, redoc_url=None, openapi_url=None)

    async def empty_trash() -> None:
        if left := storage.empty_trash():
            log.info("storage: removing %d folder(s) an earlier run deleted", left)

    app.router.on_startup.append(empty_trash)

    @app.exception_handler(AdminError)
    async def _admin_error(_req: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse({"error": True, "msg": exc.msg, **exc.extra}, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(splices.SpliceError)
    async def _splice_error(req: Request, exc: splices.SpliceError) -> JSONResponse:
        return await _admin_error(req, AdminError(exc.status, exc.msg, extra=exc.extra))

    @app.exception_handler(StorageError)
    async def _storage_error(req: Request, exc: StorageError) -> JSONResponse:
        return await _admin_error(req, AdminError(exc.status, exc.msg))

    @app.exception_handler(jobs.JobNotFound)
    async def _job_not_found(req: Request, _exc: jobs.JobNotFound) -> JSONResponse:
        return await _admin_error(req, AdminError(404, "No such job"))

    @app.exception_handler(jobs.JobConflict)
    async def _job_conflict(req: Request, exc: jobs.JobConflict) -> JSONResponse:
        return await _admin_error(req, AdminError(409, str(exc)))

    @app.exception_handler(jobs.InvalidJob)
    async def _invalid_job(req: Request, exc: jobs.InvalidJob) -> JSONResponse:
        return await _admin_error(req, AdminError(400, str(exc)))

    # ── Auth: API key or session cookie ───────────────────────────────────

    passwords = AdminAuth(settings.admin_password.get_secret_value() or None, store=sessions)
    login_limiter = LoginLimiter()
    trusted_proxies = parse_networks(settings.admin_trusted_proxies)
    password_networks = parse_password_networks(settings.admin_password_networks)
    signin = signin or VexAuth.from_settings(settings)
    twitch_ids = {str(i) for i in settings.admin_twitch_ids}
    pending = PendingStates()
    site_origins = {o.strip().rstrip("/").lower() for o in settings.admin_site_origins if o.strip()}
    # The site this worker signs in for (its callback's origin) is always one: its own pages reach
    # /admin/session through the site's proxy, and a browser sends Origin with every POST.
    if own := origin_of(settings.admin_auth_redirect_url):
        site_origins.add(own)

    def site_origin(origin: str) -> bool:
        return origin.rstrip("/").lower() in site_origins

    def session_cookie_args(request: Request) -> dict[str, Any]:
        return LAN_SESSION_COOKIE_ARGS if plain_http(request, trusted_proxies) else SESSION_COOKIE_ARGS

    app.state.admin_sessions = passwords  # for tests
    started_at = dt.datetime.now(dt.UTC)

    async def live_session(token: str | None) -> Session | None:
        """The cookie's session, unless it has expired or its vex-auth session was signed out.
        If vex-auth can't be reached, the session stands and is checked again next time."""
        session = await passwords.session(token)
        if session is None or session.sid is None or signin is None:
            return session
        now = passwords.clock()
        if now - session.checked_at < CHECK_S:
            return session
        try:
            active = await signin.active(session.sid)
        except SignInError as exc:
            log.warning("could not check the admin sign-in: %s", exc)
            return session
        if not active:
            await passwords.logout(token)
            return None
        await passwords.checked(session, now)
        return session

    legacy_schemes: set[str] = set()  # the non-Bearer words already warned about

    async def verify(request: Request) -> None:
        header = request.headers.get("authorization")
        if header:
            # Legacy parser: "<anything> <key>"; "Bearer <key>" is the documented form (ADR 0003).
            scheme, _, key = header.partition(" ")
            expected = settings.admin_api_key.get_secret_value()
            if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
                raise AdminError(403, "Not authorized")
            if scheme.lower() != "bearer" and scheme not in legacy_schemes:
                legacy_schemes.add(scheme)  # once per word per process: scripts call often
                log.warning(
                    "an API-key request used 'Authorization: %s <key>'; only 'Bearer <key>' will be "
                    "accepted from the next release",
                    scheme[:20],
                )
            request.state.actor = "api-key"
            _actor.set(actor_of("api-key"))
            return
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise Unauthenticated("Missing auth key")
        session = await live_session(token)
        if session is None:
            raise Unauthenticated("Session expired; log in again")
        # Known from here, so a refused write still says who (the v2 refusals audit).
        request.state.actor = session.actor
        request.state.actor_login = login_of(session)
        _actor.set(actor_of(session.actor, login_of(session)))
        if request.method not in SAFE_METHODS and not passwords.valid_csrf(session, request.headers.get(CSRF_HEADER)):
            raise AdminError(403, "Missing or wrong X-CSRF-Token")

    auth = [Depends(verify)]

    async def verify_v2(request: Request) -> None:
        """``verify`` for /api/v2: ``request.state.actor`` is an ``Actor``, and a refusal is an
        ``ApiError`` (401 without credentials, 403 with wrong ones)."""
        try:
            await verify(request)
        except AdminError as exc:
            raise ApiError(401 if isinstance(exc, Unauthenticated) else 403, detail=exc.msg) from None
        finally:
            if isinstance(actor := getattr(request.state, "actor", None), str):
                request.state.actor = actor_of(actor, getattr(request.state, "actor_login", None))

    # ── Audit log ─────────────────────────────────────────────────────────

    @app.middleware("http")
    async def audit_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in SAFE_METHODS or not request.url.path.startswith(AUDITED_PREFIXES):
            return await call_next(request)
        body = await request.body()  # cached by Starlette, so the route can still read it
        response = await call_next(request)
        actor = getattr(request.state, "actor", None)  # set once the request is authenticated
        if actor and response.status_code < 400 and not request.url.path.startswith(JOB_ROUTES):
            try:
                await audit(request, actor, body, getattr(request.state, "actor_login", None))
            except Exception:
                log.exception("could not write the audit log for %s %s", request.method, request.url.path)
        return response

    async def audit(request: Request, actor: str, raw: bytes, actor_login: str | None = None) -> None:
        route = request.scope.get("route")
        params = request.scope.get("path_params") or {}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "password"}
        detail = getattr(request.state, "audit_detail", None)
        target = getattr(request.state, "audit_target", None)  # set by routes whose params don't say it
        if target is not None:
            pass
        elif "vod_id" in params:
            target = f"vod:{params['vod_id']}"
        elif "job_id" in params:
            target = f"job:{params['job_id']}"
        elif "key" in params:
            target = f"setting:{params['key']}"
        elif "area" in params:
            target = f"storage:{params['area']}/{params.get('name')}"
        elif isinstance(body, dict) and body.get("vodId") not in (None, ""):
            target = f"vod:{body['vodId']}"
        await write_audit(
            route_entry(
                f"{request.method} {getattr(route, 'path', request.url.path)}",
                actor_of(actor, actor_login),
                target,
                body if detail is None else detail,
            )
        )

    # ── CORS for /admin/session: the configured site origins only ────────

    @app.middleware("http")
    async def session_cors(request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        if request.url.path != SESSION_PATH or not origin:
            return await call_next(request)
        allowed = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
        if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
            if not site_origin(origin):
                return Response(status_code=403, headers={"Vary": "Origin"})
            return Response(status_code=204, headers={**allowed, **CORS_HEADERS, "Vary": "Origin"})
        response = await call_next(request)
        response.headers.append("Vary", "Origin")
        if site_origin(origin):
            response.headers.update(allowed)
        return response

    # ── Session (browser login) ───────────────────────────────────────────

    def password_here(request: Request) -> bool:
        return (
            passwords.enabled
            and not forwarded_untrusted(request, trusted_proxies)
            and password_allowed(client_address(request, trusted_proxies), password_networks)
        )

    def session_json(request: Request, session: Session | None) -> dict[str, Any]:
        return {
            "authenticated": session is not None,
            "csrf": session.csrf if session else None,
            "expiresAt": iso_utc(dt.datetime.fromtimestamp(session.expires_at, dt.UTC)) if session else None,
            "passwordLogin": password_here(request),  # offered to this address
            "twitchLogin": signin is not None,
            "user": session.user if session else None,  # the Twitch user; null for a password login
        }

    @app.get("/admin/session")
    async def get_session(request: Request) -> dict[str, Any]:
        return session_json(request, await live_session(request.cookies.get(SESSION_COOKIE)))

    @app.post("/admin/session")
    async def login(request: Request, body: dict[str, Any] | None = Body(None)) -> Response:
        if isinstance(body, dict) and "code" in body:
            return await code_login(request, body["code"])
        if not passwords.enabled:
            raise AdminError(404, "Password login is off (ARCHIVE_ADMIN_PASSWORD is not set)")
        address = client_address(request, trusted_proxies)
        if forwarded_untrusted(request, trusted_proxies):
            # Fail closed: through an unlisted proxy every client would pass as the proxy's own address.
            log.warning(
                "admin password refused: %s forwards requests but is not in ARCHIVE_ADMIN_TRUSTED_PROXIES", address
            )
            raise AdminError(
                403,
                "The password is off through this proxy until it is listed in ARCHIVE_ADMIN_TRUSTED_PROXIES; "
                "sign in with Twitch",
            )
        if not password_allowed(address, password_networks):
            log.warning("admin password refused from %s (not in ARCHIVE_ADMIN_PASSWORD_NETWORKS)", address)
            raise AdminError(403, "The password only works from the local network; sign in with Twitch")
        wait = login_limiter.retry_after(address)
        if wait is not None:
            raise AdminError(429, "Too many failed logins; try again later", {"Retry-After": str(wait)})
        password = (body or {}).get("password")
        if not isinstance(password, str) or not password:
            raise AdminError(400, "Missing parameter: password")
        if not await asyncio.to_thread(passwords.check_password, password):  # scrypt: keep it off the loop
            login_limiter.failed(address)
            log.warning("failed admin login from %s", address)
            raise AdminError(401, "Wrong password")
        login_limiter.reset(address)
        await passwords.logout(request.cookies.get(SESSION_COOKIE))
        session = await passwords.login()
        request.state.actor = "password"
        response = JSONResponse(session_json(request, session))
        response.set_cookie(SESSION_COOKIE, session.token, max_age=int(passwords.ttl_s), **session_cookie_args(request))
        return response

    def refused(status: int, code: str, detail: str) -> Response:
        """A code sign-in's refusal, as RFC 9457 problem details."""
        response: Response = problem(status, code, detail)
        return response

    async def code_login(request: Request, code: Any) -> Response:
        """``POST /admin/session {code}``: a Twitch sign-in from a code the site minted with vex-auth's
        ``POST /v1/codes``, without a redirect. An admin gets the dashboard session the callback would
        make; anyone else ``{"admin": false}`` and no cookie. Refusals are problem details."""
        origin = request.headers.get("origin")
        if origin is not None and not site_origin(origin):
            log.warning("admin code sign-in refused from origin %s (not in ARCHIVE_ADMIN_SITE_ORIGINS)", origin)
            return refused(403, "origin_not_allowed", "This origin is not one of ARCHIVE_ADMIN_SITE_ORIGINS")
        if signin is None:
            return refused(
                404, "signin_off", "Twitch sign-in is off (see ARCHIVE_ADMIN_AUTH_* in docs/configuration.md)"
            )
        if not isinstance(code, str) or not code:
            return refused(400, "invalid_code", "Missing parameter: code")
        try:
            signed_in = await signin.redeem(code, fetched=True)
        except SignInError as exc:
            log.warning("admin code sign-in failed (%s): %s", exc.reason, exc)
            if exc.reason == "expired":
                return refused(400, "invalid_code", "The code is unknown, expired or already used")
            if exc.reason == "misconfigured":
                return refused(502, "misconfigured", "vex-auth refused this worker's client credentials")
            return refused(503, "unavailable", "vex-auth could not be reached; try again")
        user_id = str(signed_in.user.get("id", ""))
        if user_id not in twitch_ids:
            # Every signed-in viewer's check: not worth a warning.
            log.info("admin code sign-in: twitch:%s (%s) is not an admin", user_id, signed_in.user.get("login"))
            return JSONResponse({"admin": False})
        await passwords.logout(request.cookies.get(SESSION_COOKIE))
        session = await passwords.login(f"twitch:{user_id}", signed_in.user, signed_in.sid)
        request.state.actor = session.actor  # audited as session.login, without the code
        request.state.actor_login = login_of(session)
        request.state.audit_detail = {"via": "code"}
        response = JSONResponse({"admin": True, **session_json(request, session)})
        cookie: dict[str, Any] = SESSION_COOKIE_ARGS  # the callback's: always Secure
        response.set_cookie(SESSION_COOKIE, session.token, max_age=int(passwords.ttl_s), **cookie)
        return response

    # ── Twitch sign-in (through vex-auth) ─────────────────────────────
    # The callback is reached through a redirect from another site, which a SameSite=Strict
    # cookie would not be sent on, so the state cookie is Lax. The session cookie stays Strict:
    # it is set here and only read by the dashboard's own requests.

    @app.get("/admin/signin")
    async def signin_start(next: str | None = None) -> Response:
        if signin is None:
            raise AdminError(404, "Twitch sign-in is off (see ARCHIVE_ADMIN_AUTH_* in docs/configuration.md)")
        state = pending.start(safe_next(next))
        response = RedirectResponse(signin.authorize_url(state), status_code=302)
        response.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL_S, **STATE_COOKIE_ARGS)  # type: ignore[arg-type]
        return response

    @app.get("/admin/signin/callback")
    async def signin_callback(
        request: Request, code: str | None = None, state: str | None = None, error: str | None = None
    ) -> Response:
        if signin is None:
            raise AdminError(404, "Twitch sign-in is off (see ARCHIVE_ADMIN_AUTH_* in docs/configuration.md)")
        cookie = request.cookies.get(STATE_COOKIE) or ""
        started = pending.finish(state)
        # The state must be one this worker handed out, to this browser.
        if started is None or not secrets.compare_digest(cookie.encode(), (state or "").encode()):
            return signin_failed("expired", "/admin")
        next_path = started.next
        if error or not code:
            return signin_failed(error if error in ERRORS else "twitch", next_path)
        try:
            signed_in = await signin.redeem(code)
        except SignInError as exc:
            log.warning("admin sign-in failed (%s): %s", exc.reason, exc)
            return signin_failed(exc.reason, next_path)
        user_id = str(signed_in.user.get("id", ""))
        if user_id not in twitch_ids:
            log.warning("admin sign-in refused for twitch:%s (%s)", user_id, signed_in.user.get("login"))
            return signin_failed("not_allowed", next_path)
        await passwords.logout(request.cookies.get(SESSION_COOKIE))
        session = await passwords.login(f"twitch:{user_id}", signed_in.user, signed_in.sid)
        try:
            await audit(request, session.actor, b"", login_of(session))
        except Exception:
            log.exception("could not write the audit log for a sign-in")
        response = RedirectResponse(next_path, status_code=302)
        response.set_cookie(SESSION_COOKIE, session.token, max_age=int(passwords.ttl_s), **SESSION_COOKIE_ARGS)  # type: ignore[arg-type]
        response.delete_cookie(STATE_COOKIE, **STATE_COOKIE_ARGS)  # type: ignore[arg-type]
        return response

    def signin_failed(reason: str, next_path: str) -> Response:
        """Back to the dashboard's login page, which explains ``auth_error``."""
        target = f"/admin/login?{urlencode({'auth_error': reason, 'next': next_path})}"
        response = RedirectResponse(target, status_code=302)
        response.delete_cookie(STATE_COOKIE, **STATE_COOKIE_ARGS)  # type: ignore[arg-type]
        return response

    @app.delete("/admin/session", status_code=204)
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        session = await passwords.session(token)
        if session is not None:
            if not passwords.valid_csrf(session, request.headers.get(CSRF_HEADER)):
                raise AdminError(403, "Missing or wrong X-CSRF-Token")
            await passwords.logout(token)
            request.state.actor = session.actor
            request.state.actor_login = login_of(session)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, **session_cookie_args(request))
        return response

    def job_action(msg: str, job: Any) -> dict[str, Any]:
        """An admin action on a job: noted in the job's events, answered like any action."""
        service.note(job, msg)
        return _ok(msg, job)

    async def vod_exists(vod_id: str) -> Vod | None:
        async with get_sessionmaker()() as s:
            return await s.get(Vod, str(vod_id))  # type: ignore[no-any-return]

    async def require_vod(vod_id: str) -> Vod:
        vod = await vod_exists(vod_id)
        if vod is None:
            raise AdminError(404, "No such VOD")
        return vod

    async def refuse_spliced(vod_id: str, what: str) -> None:
        """Actions that refetch from Twitch by VOD id, or need the VOD's one source video,
        do not fit a merged or split VOD (its row no longer matches Twitch's VOD)."""
        reason = await splice_reason(str(vod_id))
        if reason:
            raise AdminError(409, f"{reason}; {what} would refetch or replace it. Undo the merge/split first")

    async def enqueue(kind: str, vod_id: str | None, payload: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """``service.enqueue``, refused for a job with a TWITCH_STEPS step on a merged or split VOD."""
        if vod_id is not None and TWITCH_STEPS & set(jobs.KINDS.get(kind, [])):
            await refuse_spliced(vod_id, f"a {kind} job")
        return await service.enqueue(kind, vod_id, payload, actor=_actor.get(), **kwargs)

    def require_helix() -> None:
        if not helix.configured:
            raise AdminError(500, "Twitch client is not configured")

    async def helix_video(vod_id: str) -> dict[str, Any]:
        require_helix()
        video = await helix.get_video(str(vod_id))
        if not video:
            raise AdminError(404, "No such VOD")
        if str(video.get("user_id")) != str(settings.twitch_id):
            raise AdminError(400, "This VOD belongs to another channel")
        return video

    def vtype(body: dict[str, Any]) -> str:
        t = body.get("type") or "vod"
        if t not in vod_edits.VIDEO_TYPES:
            raise AdminError(400, "type must be 'vod' or 'live'")
        return t

    def type_payload(vod: Vod, body: dict[str, Any]) -> dict[str, Any]:
        t = vtype(body)
        payload: dict[str, Any] = {"type": t, "manual": True}  # started from the dashboard (force_upload_manual)
        if t == "live":
            if not vod.stream_id:
                raise AdminError(400, "This VOD has no stream_id, so its live recording can't be found")
            payload["stream_id"] = vod.stream_id
        return payload

    def source_payload(vod: Vod, body: dict[str, Any]) -> dict[str, Any]:
        """type_payload plus an optional local ``path`` to use instead of downloading."""
        payload = type_payload(vod, body)
        if body.get("path"):
            payload["path"] = body["path"]
        return payload

    def single_part(payload: dict[str, Any], body: dict[str, Any]) -> int:
        part = int(body["part"])
        payload.update(start_part=part, end_part=part)
        return part

    async def viewer_check(fresh: bool = False) -> dict[str, Any] | None:
        """The linked account for the dashboard (``twitchViewer`` in /admin/health), or None if the
        check itself failed."""
        try:
            st = await (deps.viewer.check() if fresh else deps.viewer.cached_check(YOUTUBE_CHECK_MAX_AGE))
        except Exception:
            log.exception("could not check the Twitch viewer token")
            return None
        return {**st, "checkedAt": iso_utc(st["checkedAt"])}

    ctx = AdminContext(
        deps=deps,
        settings=settings,
        service=service,
        runtime=runtime,
        storage=storage,
        helix=helix,
        auth=auth,
        started_at=started_at,
        enqueue=enqueue,
        job_action=job_action,
        vod_exists=vod_exists,
        require_vod=require_vod,
        refuse_spliced=refuse_spliced,
        require_helix=require_helix,
        helix_video=helix_video,
        type_payload=type_payload,
        source_payload=source_payload,
        single_part=single_part,
        viewer_check=viewer_check,
    )
    # In this order: FastAPI matches routes in the order they were added (e.g. /admin/jobs/{job_id}
    # after the fixed paths under it, all within admin_jobs).
    for module in (admin_status, admin_site, admin_jobs, admin_vods, admin_twitch, admin_pipelines, admin_youtube):
        module.register(app, ctx)

    # ── /api/v2 ───────────────────────────────────────────────────────────

    api_v2.mount(
        app,
        auth=verify_v2,
        runtime=service.runtime,
        vod_exists=vod_exists,
        twitch_steps=TWITCH_STEPS,
        routers=[
            api_v2_routes.settings_router(runtime, service.apply_settings, verify_v2),
            api_v2_routes.storage_router(storage, verify_v2),
            api_v2_routes.vods_router(verify_v2),
        ],
    )

    return app
