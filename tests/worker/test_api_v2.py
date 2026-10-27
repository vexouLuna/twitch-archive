"""/api/v2: auth, problem details, request ids, the job and audit routes, docs (needs the dev DB)."""

import datetime as dt
from typing import Any

import httpx
import pytest
from archive_common.audit import AUDIT_LOG
from archive_common.db import get_sessionmaker
from archive_common.models import Emote, RuntimeSetting, Vod
from archive_worker import jobs
from archive_worker.admin import create_admin_app
from archive_worker.admin_auth import CSRF_HEADER, SESSION_COOKIE
from archive_worker.job_rows import RUNS, subject_of
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text

VOD = "test-api-v2-vod"
KEY = {"Authorization": "Bearer k"}


async def _reset():
    async with get_sessionmaker()() as s:
        runs = select(text("'job:' || id")).select_from(RUNS).where(RUNS.c.subject == subject_of(VOD))
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.target.in_(runs)))
        await s.execute(
            delete(AUDIT_LOG).where(AUDIT_LOG.c.action == "request.denied", text("detail->>'path' LIKE '/api/v2/%'"))
        )
        await s.execute(delete(RUNS).where(RUNS.c.subject == subject_of(VOD)))
        await s.execute(text("SET LOCAL search_path TO jobs, public"))
        await s.execute(text("DELETE FROM procrastinate_jobs WHERE lock LIKE :vod"), {"vod": f"vod:{VOD}:%"})
        await s.execute(delete(Emote).where(Emote.vod_id == VOD))
        await s.execute(delete(Vod).where(Vod.id == VOD))
        await s.commit()


@pytest.fixture
async def vod(db):
    await _reset()
    async with get_sessionmaker()() as s:
        s.add(Vod(id=VOD, title="t", created_at=dt.datetime.now(dt.UTC), duration="00:10:00"))
        await s.commit()
    yield VOD
    await _reset()


@pytest.fixture
async def admin(vod, deps, make_service, monkeypatch):
    """An admin app whose runtime knows kind "test" (steps a, b) and "refetch" (a TWITCH_STEPS step);
    nothing runs them."""
    monkeypatch.setitem(jobs.KINDS, "test", ["a", "b"])
    monkeypatch.setitem(jobs.KINDS, "refetch", ["chat"])
    for name in "ab":
        monkeypatch.setitem(jobs.STEPS, name, lambda ctx: None)  # type: ignore[attr-defined]
    deps.settings.admin_api_key = SecretStr("k")
    async with get_sessionmaker()() as s:
        mark = (await s.execute(select(func.max(AUDIT_LOG.c.id)))).scalar() or 0
    app = create_admin_app(deps, await make_service(start=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://admin") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c
    async with get_sessionmaker()() as s:  # what the settings, storage and VOD routes audited
        await s.execute(delete(AUDIT_LOG).where(AUDIT_LOG.c.id > mark))
        await s.execute(delete(RuntimeSetting).where(RuntimeSetting.key == "keep_hls"))
        await s.commit()


def _problem(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert (body["status"], body["code"], body["request_id"]) == (status, code, r.headers["x-request-id"])
    return body  # type: ignore[no-any-return]


async def test_callers_are_refused_as_problems(admin):
    _problem(await admin.get("/api/v2/jobs"), 401, "unauthenticated")
    _problem(await admin.get("/api/v2/jobs", headers={"Authorization": "Bearer nope"}), 403, "forbidden")
    admin.cookies.set(SESSION_COOKIE, "gone")
    _problem(await admin.get("/api/v2/jobs"), 401, "unauthenticated")
    admin.cookies.clear()
    # v1 answers as it always has.
    r = await admin.get("/admin/jobs")
    assert (r.status_code, r.json()) == (403, {"error": True, "msg": "Missing auth key"})


async def test_a_session_write_without_csrf_is_refused_and_audited(admin):
    session = await admin.app.state.admin_sessions.login("twitch:100", user={"login": "alice"})
    admin.cookies.set(SESSION_COOKIE, session.token)
    r = await admin.post("/api/v2/jobs", json={"kind": "test", "subject": f"vod:{VOD}"})
    _problem(r, 403, "forbidden")
    async with get_sessionmaker()() as s:
        row = (await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.request_id == r.headers["x-request-id"]))).one()
    assert (row.action, row.outcome, row.actor_kind, row.actor_id, row.actor_login, row.detail) == (
        "request.denied",
        "denied",
        "user",
        "100",
        "alice",
        {"method": "POST", "path": "/api/v2/jobs", "status": 403},
    )

    r = await admin.post(
        "/api/v2/jobs",
        headers={CSRF_HEADER: session.csrf},
        json={"kind": "test", "subject": f"vod:{VOD}", "paused": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["actor"] == {"kind": "user", "id": "100", "login": "alice", "via": "web"}


async def test_jobs_queue_list_cancel_and_audit_with_the_request_id(admin):
    r = await admin.post(
        "/api/v2/jobs",
        headers={**KEY, "X-Request-ID": "req-1"},
        json={"kind": "test", "subject": f"vod:{VOD}", "paused": True},
    )
    assert (r.status_code, r.headers["x-request-id"]) == (201, "req-1")
    run = r.json()
    assert (run["kind"], run["state"], run["step"], run["steps"], run["actor"]["kind"]) == (
        "test",
        "paused",
        "a",
        ["a", "b"],
        "api_key",
    )

    page = (await admin.get(f"/api/v2/jobs?subject=vod:{VOD}", headers=KEY)).json()
    assert ([j["id"] for j in page["items"]], page["next_cursor"]) == ([run["id"]], None)
    cancel = await admin.post(f"/api/v2/jobs/{run['id']}/cancel", headers=KEY)
    assert cancel.json()["state"] == "cancelled"
    _problem(await admin.post(f"/api/v2/jobs/{run['id']}/resume", headers=KEY), 409, "job_conflict")
    _problem(await admin.get("/api/v2/jobs/999999999", headers=KEY), 404, "job_not_found")

    audit = (await admin.get(f"/api/v2/audit?target=job:{run['id']}", headers=KEY)).json()["items"]
    assert [(a["action"], a["actor_kind"], a["request_id"]) for a in audit] == [
        ("job.cancel", "api_key", cancel.headers["x-request-id"]),
        ("job.enqueue", "api_key", "req-1"),
    ]
    assert audit[1]["at"].endswith("Z")


async def test_queueing_checks_the_vod(admin):
    body = {"kind": "test", "paused": True}
    r = await admin.post("/api/v2/jobs", headers=KEY, json={**body, "subject": "channel:1"})
    _problem(r, 422, "invalid_subject")
    _problem(await admin.post("/api/v2/jobs", headers=KEY, json={**body, "subject": "vod:nope"}), 404, "vod_not_found")
    _problem(
        await admin.post("/api/v2/jobs", headers=KEY, json={**body, "kind": "nope", "subject": f"vod:{VOD}"}),
        422,
        "invalid_job",
    )
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).merged_into = {"id": "other"}
        await s.commit()
    r = await admin.post("/api/v2/jobs", headers=KEY, json={"kind": "refetch", "subject": f"vod:{VOD}"})
    assert "merged into other" in _problem(r, 409, "vod_spliced")["detail"]
    # A kind that does not refetch from Twitch is still fine on a merged VOD.
    assert (await admin.post("/api/v2/jobs", headers=KEY, json={**body, "subject": f"vod:{VOD}"})).status_code == 201


async def test_invalid_bodies_list_their_errors(admin):
    body = _problem(await admin.post("/api/v2/jobs", headers=KEY, json={"subject": 1}), 422, "invalid")
    assert ["body", "kind"] in [e["loc"] for e in body["errors"]]


async def test_docs_are_behind_auth_and_cover_v2_only(admin):
    _problem(await admin.get("/api/v2/docs"), 401, "unauthenticated")
    _problem(await admin.get("/api/v2/openapi.json"), 401, "unauthenticated")
    assert "swagger-ui" in (await admin.get("/api/v2/docs", headers=KEY)).text
    paths = (await admin.get("/api/v2/openapi.json", headers=KEY)).json()["paths"]
    assert {
        "/api/v2/jobs",
        "/api/v2/jobs/{run_id}/cancel",
        "/api/v2/audit",
        "/api/v2/job-kinds",
        "/api/v2/settings",
        "/api/v2/storage/{area}/{name}",
        "/api/v2/vods",
        "/api/v2/vods/{vod_id}",
    } <= set(paths)
    assert not [p for p in paths if not p.startswith("/api/v2/") or p.endswith(("/docs", "/openapi.json"))]
    assert (await admin.get("/openapi.json", headers=KEY)).status_code == 404  # still off for the rest


async def _audit(request_id: str) -> dict[str, Any]:
    async with get_sessionmaker()() as s:
        return dict((await s.execute(select(AUDIT_LOG).where(AUDIT_LOG.c.request_id == request_id))).mappings().one())


async def test_settings_change_and_reset_with_their_audit(admin):
    items = (await admin.get("/api/v2/settings", headers=KEY)).json()["items"]
    keep = next(i for i in items if i["key"] == "keep_hls")
    assert (keep["value"], keep["overridden"], keep["updated_at"]) == (False, False, None)
    assert next(i for i in items if i["key"] == "manual_steps")["choices"]["test"] == ["a", "b"]

    r = await admin.patch("/api/v2/settings", headers=KEY, json={"keep_hls": True})
    keep = next(i for i in r.json()["items"] if i["key"] == "keep_hls")
    assert (keep["value"], keep["overridden"], keep["updated_by"]) == (True, True, "api-key")
    assert keep["updated_at"].endswith("Z")
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["target"], row["actor_kind"], row["before"], row["after"]) == (
        "setting.update",
        None,
        "api_key",
        {"keep_hls": False},
        {"keep_hls": True},
    )

    _problem(await admin.patch("/api/v2/settings", headers=KEY, json={"keep_hls": 1}), 422, "invalid_setting")
    _problem(await admin.delete("/api/v2/settings/nope", headers=KEY), 404, "setting_not_found")
    r = await admin.delete("/api/v2/settings/keep_hls", headers=KEY)
    assert next(i for i in r.json()["items"] if i["key"] == "keep_hls")["value"] is False
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["target"], row["before"], row["after"]) == (
        "setting.reset",
        "setting:keep_hls",
        {"keep_hls": True},
        {"keep_hls": False},
    )


async def test_storage_delete_is_audited(admin, settings):
    folder = settings.data_dir / "vods" / VOD
    folder.mkdir(parents=True)
    (folder / "part.mp4").write_bytes(b"12345")
    view = (await admin.get("/api/v2/storage?refresh=true", headers=KEY)).json()
    mine = next(f for f in view["folders"] if f["name"] == VOD)
    assert (mine["path"], mine["bytes"], mine["files"], mine["vod"]["id"], mine["jobs"]) == (
        f"vods/{VOD}",
        5,
        1,
        VOD,
        {"active": [], "last": None},
    )
    assert mine["modified_at"].endswith("Z") and "cache_seconds" in view

    r = await admin.delete(f"/api/v2/storage/vods/{VOD}", headers=KEY)
    assert r.json() == {"path": f"vods/{VOD}", "bytes": 5, "files": 1}
    assert not folder.exists()
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["target"], row["detail"]) == ("storage.delete", f"storage:vods/{VOD}", r.json())
    _problem(await admin.delete(f"/api/v2/storage/vods/{VOD}", headers=KEY), 404, "folder_not_found")


async def test_vods_list_says_what_is_uploaded_and_running(admin):
    row = (await admin.get(f"/api/v2/vods?q={VOD}", headers=KEY)).json()["items"][0]
    assert (row["uploaded"], row["active_jobs"]) == (False, [])

    body = {"subject": f"vod:{VOD}", "paused": True}
    run = (await admin.post("/api/v2/jobs", headers=KEY, json={**body, "kind": "test"})).json()
    done = (await admin.post("/api/v2/jobs", headers=KEY, json={**body, "kind": "refetch"})).json()
    await admin.post(f"/api/v2/jobs/{done['id']}/cancel", headers=KEY)
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).youtube = [{"id": "yt1"}]
        await s.commit()

    row = (await admin.get(f"/api/v2/vods?q={VOD}", headers=KEY)).json()["items"][0]
    assert row["uploaded"] is True
    assert row["active_jobs"] == [{"id": run["id"], "kind": "test", "state": "paused", "step": "a"}]
    assert (await admin.get(f"/api/v2/vods/{VOD}", headers=KEY)).json()["active_jobs"] == row["active_jobs"]


async def test_vods_list_filters_on_upload_and_active_jobs(admin):
    async def ids(query):
        return [v["id"] for v in (await admin.get(f"/api/v2/vods?q={VOD}&{query}", headers=KEY)).json()["items"]]

    assert (await ids("uploaded=false&busy=false"), await ids("uploaded=true"), await ids("busy=true")) == (
        [VOD],
        [],
        [],
    )
    await admin.post("/api/v2/jobs", headers=KEY, json={"subject": f"vod:{VOD}", "kind": "test", "paused": True})
    assert (await ids("busy=true"), await ids("busy=false")) == ([VOD], [])
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).youtube = [{"id": "yt1"}]
        await s.commit()
    assert (await ids("uploaded=true"), await ids("uploaded=false")) == ([VOD], [])


async def test_vods_list_get_and_update_with_their_audit(admin):
    page = (await admin.get(f"/api/v2/vods?q={VOD}", headers=KEY)).json()
    assert ([v["id"] for v in page["items"]], page["next_cursor"]) == ([VOD], None)
    assert page["items"][0]["duration_seconds"] == 600
    first = (await admin.get("/api/v2/vods?limit=1", headers=KEY)).json()
    if first["next_cursor"]:  # the dev DB holds more VODs: the next page starts after the first
        second = (await admin.get(f"/api/v2/vods?limit=1&cursor={first['next_cursor']}", headers=KEY)).json()
        assert second["items"][0]["id"] != first["items"][0]["id"]
    _problem(await admin.get("/api/v2/vods?cursor=nope", headers=KEY), 400, "bad_cursor")

    vod = (await admin.get(f"/api/v2/vods/{VOD}", headers=KEY)).json()
    assert (vod["chapters"], vod["chapters_locked"], vod["created_at"][-1]) == ([], False, "Z")
    _problem(await admin.get("/api/v2/vods/nope", headers=KEY), 404, "vod_not_found")

    r = await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"title": " new ", "thumbnail_url": None})
    assert (r.json()["title"], r.json()["thumbnail_url"]) == ("new", None)
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["target"], row["before"], row["after"]) == (
        "vod.update",
        f"vod:{VOD}",
        {"thumbnail_url": None, "title": "t"},
        {"thumbnail_url": None, "title": "new"},
    )

    _problem(await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"duration": "1h"}), 422, "invalid_vod")
    _problem(await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"nope": 1}), 422, "invalid")
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).merged_into = {"id": "other"}
        await s.commit()
    _problem(await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"title": "x"}), 409, "vod_merged")
    assert (await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"hidden": True})).json()["hidden"] is True


async def test_chapters_replace_with_lock_and_audit(admin):
    chapters = [
        {"name": "Celeste", "gameId": "504461", "start": 0, "length": 120, "restricted": False},
        {"name": "Just Chatting", "gameId": "509658", "start": 120, "length": 480, "restricted": False},
    ]
    r = await admin.put(f"/api/v2/vods/{VOD}/chapters", headers=KEY, json={"chapters": chapters, "locked": True})
    assert r.status_code == 200, r.text
    vod = r.json()
    assert vod["chapters_locked"] is True
    stored = [(c["name"], c["start"], c["end"]) for c in vod["chapters"]]  # the legacy shape: end is the length
    assert stored == [
        ("Celeste", 0, 120),
        ("Just Chatting", 120, 480),
    ]
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["before"]) == ("vod.chapters.replace", {"chapters": [], "chapters_locked": False})
    assert row["after"]["chapters_locked"] is True and len(row["after"]["chapters"]) == 2

    _problem(
        await admin.put(f"/api/v2/vods/{VOD}/chapters", headers=KEY, json={"chapters": chapters[::-1], "locked": True}),
        422,
        "invalid_chapters",
    )
    _problem(await admin.put(f"/api/v2/vods/{VOD}/chapters", headers=KEY, json={"chapters": chapters}), 422, "invalid")
    _problem(
        await admin.put("/api/v2/vods/nope/chapters", headers=KEY, json={"chapters": [], "locked": False}),
        404,
        "vod_not_found",
    )
    r = await admin.put(f"/api/v2/vods/{VOD}/chapters", headers=KEY, json={"chapters": [], "locked": False})
    assert (r.json()["chapters"], r.json()["chapters_locked"]) == ([], False)


async def test_youtube_drive_and_censor_lists_replace_with_their_audit(admin):
    youtube = [{"id": "abc123", "type": "vod", "part": 1, "duration": 600}]
    r = await admin.put(f"/api/v2/vods/{VOD}/youtube", headers=KEY, json={"youtube": youtube})
    assert r.status_code == 200, r.text
    assert [(e["id"], e["part"], e["duration"]) for e in r.json()["youtube"]] == [("abc123", 1, 600)]
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["before"], row["after"]["youtube"][0]["id"]) == (
        "vod.youtube.replace",
        {"youtube": []},
        "abc123",
    )
    twice = [*youtube, {**youtube[0], "part": 2}]
    _problem(
        await admin.put(f"/api/v2/vods/{VOD}/youtube", headers=KEY, json={"youtube": twice}), 422, "invalid_youtube"
    )

    r = await admin.put(f"/api/v2/vods/{VOD}/drive", headers=KEY, json={"drive": [{"id": "f1", "type": "live"}]})
    assert r.json()["drive"] == [{"id": "f1", "type": "live"}]
    assert (await _audit(r.headers["x-request-id"]))["action"] == "vod.drive.replace"
    bad = {"drive": [{"id": "f1", "type": "clip"}]}
    _problem(await admin.put(f"/api/v2/vods/{VOD}/drive", headers=KEY, json=bad), 422, "invalid_drive")

    censor = [{"start": 30, "end": 60, "video": "blur", "mute": True, "note": "spoiler"}]
    r = await admin.put(f"/api/v2/vods/{VOD}/censor", headers=KEY, json={"censor": censor})
    assert r.json()["censor"] == censor
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["before"], row["after"]) == ("vod.censor.replace", {"censor": []}, {"censor": censor})
    late = {"censor": [{"start": 590, "end": 700, "mute": True}]}
    _problem(await admin.put(f"/api/v2/vods/{VOD}/censor", headers=KEY, json=late), 422, "invalid_censor")
    _problem(await admin.put("/api/v2/vods/nope/censor", headers=KEY, json={"censor": []}), 404, "vod_not_found")

    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).synthetic = {"supersedes": False}
        await s.commit()
    _problem(await admin.put(f"/api/v2/vods/{VOD}/censor", headers=KEY, json={"censor": []}), 409, "vod_synthetic")
    async with get_sessionmaker()() as s:
        vod = await s.get(Vod, VOD)
        vod.synthetic, vod.merged_into = None, {"id": "other"}
        await s.commit()
    _problem(await admin.put(f"/api/v2/vods/{VOD}/censor", headers=KEY, json={"censor": []}), 409, "vod_merged")


async def test_merge_candidates_are_the_vods_that_started_soon_after(admin):
    async with get_sessionmaker()() as s:
        vod = await s.get(Vod, VOD)
        start = vod.created_at
        s.add(Vod(id=f"{VOD}-b", title=" T ", created_at=start + dt.timedelta(minutes=12), duration="00:05:00"))
        s.add(Vod(id=f"{VOD}-c", title="t", created_at=start + dt.timedelta(hours=5), duration="00:05:00"))
        await s.commit()
    try:
        r = await admin.get(f"/api/v2/vods/{VOD}/merge-candidates", headers=KEY)
        assert r.status_code == 200, r.text
        mine = [c for c in r.json()["items"] if c["id"].startswith(VOD)]
        assert [(c["id"], c["gap"], c["titles_match"]) for c in mine] == [(f"{VOD}-b", 120, True)]
        _problem(await admin.get("/api/v2/vods/nope/merge-candidates", headers=KEY), 404, "vod_not_found")
    finally:
        async with get_sessionmaker()() as s:
            await s.execute(delete(Vod).where(Vod.id.in_([f"{VOD}-b", f"{VOD}-c"])))
            await s.commit()


async def test_emotes_are_the_saved_row_or_null(admin):
    assert (await admin.get(f"/api/v2/vods/{VOD}/emotes", headers=KEY)).json() is None
    async with get_sessionmaker()() as s:
        s.add(Emote(vod_id=VOD, ffz_emotes=[{"id": 1, "code": "x"}]))
        await s.commit()
    emotes = (await admin.get(f"/api/v2/vods/{VOD}/emotes", headers=KEY)).json()
    assert (emotes["vod_id"], emotes["ffz_emotes"], emotes["7tv_emotes"]) == (VOD, [{"id": 1, "code": "x"}], [])
    _problem(await admin.get("/api/v2/vods/nope/emotes", headers=KEY), 404, "vod_not_found")


async def test_rearchive_queues_a_replacing_upload_with_its_audit(admin):
    whole = await admin.post(f"/api/v2/vods/{VOD}/rearchive", headers=KEY, json={})
    assert whole.status_code == 202, whole.text
    part = await admin.post(f"/api/v2/vods/{VOD}/rearchive", headers=KEY, json={"type": "vod", "part": 2})
    async with get_sessionmaker()() as s:
        ids = [whole.json()["job_id"], part.json()["job_id"]]
        a, b = sorted((await s.execute(select(RUNS).where(RUNS.c.id.in_(ids)))).all(), key=lambda r: r.id)
    assert (a.kind, a.payload) == ("download", {"type": "vod", "manual": True, "replace": True})
    assert (b.kind, b.payload) == (
        "reupload",
        {"type": "vod", "manual": True, "replace": True, "start_part": 2, "end_part": 2},
    )
    row = await _audit(part.headers["x-request-id"])
    assert (row["action"], row["target"], row["detail"]) == (
        "vod.rearchive",
        f"vod:{VOD}",
        {"job_id": b.id, "type": "vod", "part": 2},
    )
    _problem(await admin.post(f"/api/v2/vods/{VOD}/rearchive", headers=KEY, json={"type": "live"}), 400, "invalid")
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).synthetic = {"supersedes": False}
        await s.commit()
    _problem(await admin.post(f"/api/v2/vods/{VOD}/rearchive", headers=KEY, json={}), 409, "vod_synthetic")


async def test_deleting_uploads_reports_each_and_rewrites_the_rest(admin, deps, monkeypatch):
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).youtube = [
            {"id": "yt1", "type": "vod", "duration": 300, "part": 1},
            {"id": "yt2", "type": "vod", "duration": 300, "part": 2},
        ]
        await s.commit()

    async def delete_video(video_id: str) -> bool:
        if video_id == "yt2":
            raise RuntimeError("quota exceeded")
        return True

    monkeypatch.setattr(deps.youtube, "delete", delete_video)
    url = f"/api/v2/vods/{VOD}/youtube/delete"
    body = _problem(await admin.post(url, headers=KEY, json={"videos": ["nope"]}), 422, "not_uploads")
    assert body["videos"] == ["nope"]
    _problem(await admin.post(url, headers=KEY, json={"videos": []}), 422, "invalid")
    r = await admin.post(url, headers=KEY, json={"videos": ["yt1", "yt2", "yt1"]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["deleted"], [f["id"] for f in got["failed"]]) == (["yt1"], ["yt2"])
    assert [e["id"] for e in got["vod"]["youtube"]] == ["yt2"]
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], [e["id"] for e in row["after"]["youtube"]]) == ("vod.youtube.delete", ["yt2"])
    async with get_sessionmaker()() as s:
        [job] = (await s.execute(select(RUNS).where(RUNS.c.id.in_(got["describe_jobs"])))).all()
    assert (job.kind, job.payload["type"]) == ("describe", "vod")


async def test_censor_review_cuts_the_flagged_games_and_resumes(admin):
    run = (
        await admin.post("/api/v2/jobs", headers=KEY, json={"subject": f"vod:{VOD}", "kind": "test", "paused": True})
    ).json()
    url = f"/api/v2/jobs/{run['id']}/censor-review"
    _problem(await admin.post(url, headers=KEY, json={"decision": "cut"}), 409, "not_in_review")
    _problem(await admin.post(url, headers=KEY, json={"decision": "skip"}), 422, "invalid")
    _problem(
        await admin.post("/api/v2/jobs/999999999/censor-review", headers=KEY, json={"decision": "upload"}),
        404,
        "job_not_found",
    )
    chapters = [
        {"name": "A", "gameId": "1", "start": 0, "end": 60},
        {"name": "B", "gameId": "2", "start": 60, "end": 60},
    ]
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).chapters = chapters
        review = {"games": [{"gameId": "2", "name": "B"}]}
        await s.execute(RUNS.update().where(RUNS.c.id == run["id"]).values(payload={"censor_review": review}))
        await s.commit()

    r = await admin.post(url, headers=KEY, json={"decision": "cut"})
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == run["id"]
    vod = (await admin.get(f"/api/v2/vods/{VOD}", headers=KEY)).json()
    assert ([c.get("restricted", False) for c in vod["chapters"]], vod["chapters_locked"]) == ([False, True], True)
    assert (await admin.get(f"/api/v2/jobs/{run['id']}", headers=KEY)).json()["state"] == "queued"
    row = await _audit(r.headers["x-request-id"])
    assert (row["action"], row["target"], row["detail"]) == (
        "job.censor_review",
        f"job:{run['id']}",
        {"decision": "cut", "vod": VOD, "cut": ["2"]},
    )


async def test_cut_reason_is_patched_and_listed(admin):
    r = await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"cut_reason": "DMCA"})
    assert (r.status_code, r.json()["cut_reason"]) == (200, "DMCA"), r.text
    row = await _audit(r.headers["x-request-id"])
    assert (row["before"], row["after"]) == ({"cut_reason": None}, {"cut_reason": "DMCA"})
    _problem(await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"cut_reason": "x" * 501}), 422, "invalid_vod")
    async with get_sessionmaker()() as s:
        (await s.get(Vod, VOD)).synthetic = {"supersedes": False}
        await s.commit()
    r = await admin.patch(f"/api/v2/vods/{VOD}", headers=KEY, json={"cut_reason": None})
    assert (r.status_code, r.json()["cut_reason"]) == (200, None), r.text
    row = (await admin.get(f"/api/v2/vods?q={VOD}", headers=KEY)).json()["items"][0]
    assert row["cut_reason"] is None
