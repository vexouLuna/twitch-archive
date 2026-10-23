"""Settings shared by archive-api and archive-worker.

Everything comes from environment variables prefixed ``ARCHIVE_`` (or a
``.env`` file in the working directory). List values are JSON, e.g.
``ARCHIVE_RESTRICTED_GAMES='["Artifact"]'``. The admin dashboard can override some of
the worker's (archive_worker/runtime_settings.py); the env value is then the default.

Secrets can also come from files: one per setting, named like its variable in lowercase
(``archive_admin_api_key``), in ``/run/secrets`` (where compose mounts them) or
``ARCHIVE_SECRETS_DIR``. A variable set in the environment wins over its file.

So a value comes from, first match wins: an override saved from the dashboard (worker only, only the
keys in runtime_settings.ENTRIES), the environment, ``.env``, its secret file, the default here.

The fields are grouped by concern (``CoreSettings``, ``TwitchSettings``, ...). ``Settings`` is all of
them (the worker's); ``ApiSettings`` is the part archive-api reads. The groups only organise the
definitions: a field is still ``settings.<name>``, set by ``ARCHIVE_<NAME>``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = Path(os.environ.get("ARCHIVE_SECRETS_DIR", "/run/secrets"))


TITLE_FIELDS = ("channel", "label", "date", "title")  # what a title template (youtube_title*) may use


def check_title(template: str) -> None:
    """ValueError unless ``template`` formats with TITLE_FIELDS only."""
    try:
        template.format(**dict.fromkeys(TITLE_FIELDS, ""))
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"a title can use only {{{'}, {'.join(TITLE_FIELDS)}}} ({exc})") from None


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARCHIVE_",
        env_file=".env",
        extra="ignore",
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
    )


# ── Groups: one per concern. Each field keeps its variable (ARCHIVE_ + its name in capitals) ──


class CoreSettings(_Base):
    """The database and logging: both services."""

    database_url: str = "postgresql+asyncpg://postgres:dev@127.0.0.1:55433/archive"
    timezone: str = "UTC"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"  # json: one object per line, for containers


class TwitchSettings(_Base):
    """Twitch's Helix app and its private GQL: both services (the api only reads ``twitch_id``)."""

    twitch_id: str = ""
    twitch_username: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: SecretStr = SecretStr("")

    # Twitch private GQL: these rotate from time to time. When downloads start
    # failing with "PersistedQueryNotFound", update the hash here.
    gql_client_id: str = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    gql_backup_client_id: str = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
    # The client id the Manage screen's "Link Twitch account" device flow asks for a token as (Twitch's
    # TV app: Twitch refuses the device flow for the web and Android ones with "invalid client"): the
    # token GQL gets for the live playback token.
    twitch_viewer_client_id: str = "ue6666qo983tsx6so1t0vnawi233wa"
    gql_hash_playback_token: str = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"
    gql_hash_comments: str = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
    gql_hash_moments: str = "7399051b2d46f528d5f0eedf8b0db8d485bb1bb4c0a2c6707be6f1290cdcb31a"
    gql_hash_nielsen: str = "2dbf505ee929438369e68e72319d1106bb3c142e295332fac157c90638968586"


class PublicApiSettings(_Base):
    """archive-api's own: where it listens, pagination, the rate limit and its caches."""

    api_host: str = "0.0.0.0"
    api_port: int = 3030
    paginate_default: int = 10
    paginate_max: int = 50
    rate_limit_points: int = 20
    rate_limit_window_seconds: int = 5
    # Peers (addresses or CIDR ranges) whose CF-Connecting-IP / X-Real-IP / X-Forwarded-For name the client
    # the rate limit counts; any other peer is counted by its own address. ["*"]: every peer (the default,
    # as before), so a client that reaches the api directly can choose its own bucket.
    api_trusted_proxies: list[str] = ["*"]
    cache_ttl_seconds: int = 300


class DataSettings(_Base):
    """The data directory: the worker writes it, the api serves previews from it."""

    data_dir: Path = Path("/data")

    @property
    def vod_dir(self) -> Path:
        return self.data_dir / "vods"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews"


class CaptureSettings(_Base):
    """What the worker captures from Twitch, and how often it looks."""

    vod_download: bool = True
    chat_download: bool = True
    live_record: bool = False
    multi_track: bool = False
    # With live_record on and multi_track off, still upload the VOD copy of an archive started from the
    # dashboard (added from Twitch, re-downloaded): a stream the live recording missed has no other copy.
    force_upload_manual: bool = False

    monitor_interval_seconds: int = 30
    hls_poll_interval_seconds: int = 60
    hls_no_change_threshold: int = 10
    live_poll_interval_seconds: float = 2.0
    live_end_threshold: int = 90  # consecutive polls without new segments (~3 min)
    segment_concurrency: int = 4


class YouTubeSettings(_Base):
    """Uploads to YouTube: titles, descriptions, visibility, the OAuth client and the checks."""

    channel: str = ""  # display name in YouTube titles
    domain_name: str = ""  # frontend host for the "Chat Replay" link
    youtube_upload: bool = True
    youtube_public: bool = False
    compile_youtube_upload: bool = False  # the compile job may upload to YouTube; off: it only makes the file
    compile_youtube_public: bool = False  # compiled uploads public instead of unlisted (youtube_public doesn't apply)
    youtube_description: str = "VOD"
    youtube_title: str = "{channel} Twitch {label} - {date}"  # planning.TITLE_FIELDS; " PART n" is added
    youtube_title_compiled: str = "{title}"  # a compiled synthetic VOD's uploads (its own title)
    youtube_keepalive_hours: float = 24.0  # periodic token refresh; see README "YouTube OAuth"
    youtube_daily_quota: int = 10_000  # the Google project's daily API units, for the dashboard's count
    youtube_check_hours: float = 24.0  # how often the uploads are checked on YouTube; 0 = only by hand

    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_url: str = "http://localhost:3031/admin/refreshtoken"

    @field_validator("youtube_title", "youtube_title_compiled")
    @classmethod
    def _title(cls, v: str) -> str:
        check_title(v)  # a bad env template fails at startup, not at the first upload
        return v


class PipelineSettings(_Base):
    """How a job runs: restricted and censored games, manual steps, splitting, kept files, previews,
    bot chat and the runner."""

    restricted_games: list[str] = []
    # censor_check: a game whose IGDB content descriptors or themes contain one of these pauses the
    # job for a censor review (empty: no check); games in censor_allow_games are never looked up.
    censor_descriptors: list[str] = ["Sexual Content", "Nudity"]
    censor_allow_games: list[str] = []
    # Steps a job pauses before until resumed (POST /admin/jobs/{id}/resume), per
    # job kind, e.g. {"archive": ["upload"]}. A job's own pauseBefore overrides it.
    # Kinds and steps: GET /admin/kinds, or jobs.KINDS in the worker.
    manual_steps: dict[str, list[str]] = {}
    split_duration: int = 10800
    keep_hls: bool = False
    keep_mp4: bool = False
    keep_files_days: float = 0  # days a pipeline's files stay before a scheduled cleanup job removes them
    dry_run: bool = False
    runner_concurrency: int = 3  # jobs run at once
    max_attempts: int = 3  # tries of a failing step before its job fails

    # Seek-bar previews (archive_common.previews): made from each uploaded part after the upload; the
    # previews_backfill job downloads older uploads from YouTube (video only, lowest sizes) for them.
    previews: bool = True
    previews_fetch_pause_seconds: int = 60  # between two backfill downloads
    previews_ytdlp_args: list[str] = []  # extra yt-dlp arguments, e.g. ["--cookies", "/secrets/yt.txt"]

    # Chat from doomtp-bot's /log API into bot_logs (the bot_chat job); empty URL = off.
    doomtp_url: str = ""  # the bot's base URL, e.g. https://bot.example.net
    doomtp_login: str = ""  # channel login on the bot; empty = twitch_username
    # A read-scope key: adds moderation entries and removed messages. Empty = the public log.
    doomtp_api_key: SecretStr = SecretStr("")


class AdminSettings(_Base):
    """The worker's admin API: keys, the password, Twitch sign-in through vex-auth, proxies, sites."""

    admin_host: str = "0.0.0.0"
    admin_port: int = 3031
    admin_api_key: SecretStr = SecretStr("")
    # Browser (dashboard) login: unset turns password login off; the API key keeps working.
    admin_password: SecretStr = SecretStr("")
    # Addresses (or CIDR ranges) of reverse proxies whose X-Forwarded-For / X-Real-IP is
    # believed when rate-limiting logins. Empty: always use the connecting address.
    admin_trusted_proxies: list[str] = []
    # Where the admin password is taken from (addresses or CIDR ranges; ["*"] = anywhere). Elsewhere it is
    # refused, and the dashboard signs in with Twitch instead. Default: this host and the private ranges.
    admin_password_networks: list[str] = [
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",  # conventions:allow-infra
        "172.16.0.0/12",  # conventions:allow-infra
        "192.168.0.0/16",  # conventions:allow-infra
        "fc00::/7",
    ]
    # Twitch sign-in through vex-auth: on when admin_auth_url, the client secret, the redirect URL
    # and at least one Twitch user id are set. Only those users get in.
    admin_twitch_ids: list[str] = []
    admin_auth_url: str = ""  # vex-auth as browsers reach it
    admin_auth_internal_url: str = ""  # as this worker reaches it; empty = admin_auth_url
    admin_auth_client_id: str = "vods-admin"
    admin_auth_client_secret: SecretStr = SecretStr("")
    # This worker's /admin/signin/callback as browsers reach it; registered with vex-auth.
    admin_auth_redirect_url: str = ""
    # The redirect URI a code from vex-auth's POST /v1/codes carries: the client's first registered
    # one. Empty = admin_auth_redirect_url.
    admin_auth_code_redirect_url: str = ""
    # Site origins (e.g. https://vods.example.net) that may call /admin/session from the browser, with
    # cookies: CORS answers only them, and a sign-in with a code from any other origin is refused.
    admin_site_origins: list[str] = []
    # Where /admin/health looks for archive-api; empty = http://127.0.0.1:<api_port>.
    api_internal_url: str = ""
    # GET /admin/vods/{id}/merge-candidates: VODs that started up to this long after one ended.
    merge_candidate_minutes: int = 30


# ── What each service reads ──────────────────────────────────────────────


class ApiSettings(CoreSettings, TwitchSettings, PublicApiSettings, DataSettings):
    """What archive-api uses. Typing it against this, not ``Settings``, keeps the worker's settings out of it."""


class Settings(ApiSettings, CaptureSettings, YouTubeSettings, PipelineSettings, AdminSettings):
    """Every setting: the worker's, and the shared ones. The dashboard can override the ones listed in
    archive_worker/runtime_settings.py's ``ENTRIES``, and only those."""


@lru_cache
def get_settings() -> Settings:
    return Settings()
