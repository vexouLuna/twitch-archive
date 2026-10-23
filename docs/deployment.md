# Deployment (Docker Compose)

[← README](../README.md)

`compose.yaml` is the whole stack: PostgreSQL 17 (`db`, on the named volume `pgdata`), the api and the worker, and two one-shot steps that every `up` runs before the services start (docs/adr/0002):

- **`migrate`** runs `alembic upgrade head` as the superuser, from the api image.
- **`roles`** runs `deploy/roles.sql`: the `archive_api` and `archive_worker` roles, their passwords (taken from `secrets/api_database_url` and `secrets/worker_database_url`) and their grants.

If either step fails, the running api and worker are left as they are. Only the compose network reaches the database; the api port is published on `ARCHIVE_PUBLISH_ADDR` and the admin port on `ARCHIVE_ADMIN_PUBLISH_ADDR` (both default `127.0.0.1`; the admin one follows `ARCHIVE_PUBLISH_ADDR` when unset). You need:

- Docker with the compose plugin.
- A data directory for the worker, set as `ARCHIVE_HOST_DATA_DIR` (default `./data`). A VOD needs about 3–3.5 GB per hour of stream while it is processed; twice that with `MULTI_TRACK`. Files are deleted after upload.
- A reverse proxy in front of the API port if the frontend is public. A proxy on another host needs `ARCHIVE_PUBLISH_ADDR=0.0.0.0` (or the address it connects to). Set `ARCHIVE_ADMIN_PUBLISH_ADDR` separately (`127.0.0.1`, or a private interface the proxy reaches): left unset it follows `ARCHIVE_PUBLISH_ADDR`, and `0.0.0.0` would publish the admin API, over plain HTTP, on every interface. See [Admin API](admin-api.md) for what the admin port may be reached through.

If the host is an unprivileged LXC container, Docker needs `nesting=1,keyctl=1`, and a bind-mounted data directory must be writable by the container's uid.

## Install

1. **Get the code and configure it:**
   ```bash
   git clone https://github.com/vexouLuna/twitch-archive.git && cd twitch-archive
   cp .env.example .env && chmod 600 .env && $EDITOR .env
   cp -r secrets.example secrets && rm secrets/README.md && $EDITOR secrets/*
   chmod 0400 secrets/* && sudo chown 1000:1000 secrets/*   # the images run as uid 1000
   ```
   Generate the three database passwords with `openssl rand -hex 32`; they go into URLs as they are.
2. **Authorize YouTube** ([YouTube OAuth setup](youtube-oauth.md)).
3. **Start:** `docker compose up -d --wait`. On an empty volume `db` creates the `archive` database, `migrate` builds the schema and `roles` creates the roles.

To start from an existing database instead, restore it before step 3 ([Restoring a dump, or moving from a native PostgreSQL](#restoring-a-dump-or-moving-from-a-native-postgresql)).

## Migrating from the old Node app

1. **Run the new API next to the old one.** Set `ARCHIVE_API_PORT` to a free port (e.g. 3032), then `docker compose up -d api`. Check it: `curl -s 'http://127.0.0.1:3032/vods?$limit=1'`, and run the contract tests against it ([Development](development.md)).
2. **Switch the reverse proxy** to the new port. Open the site and check the list page, a VOD page, chat replay and badges.
3. **Switch the worker:** stop the old app, then `docker compose up -d worker`. Only one of them may run: both would archive the same streams. Watch `docker compose logs -f worker` through the next stream.
4. **Once you're confident:** move the API back to the old port if you like, disable the old app, and remove Redis. Nothing here uses it.

**Rollback** at any point: point the proxy back at the old app, `docker compose stop worker`, and start the old app. The schema changes are additive, so the old app keeps working. The new tables are `jobs` and `app_state`, which the old app ignores.

## Updating

```bash
git pull
docker compose pull && docker compose up -d --wait
```

`migrate` and `roles` run first, against the database the old containers are still using; then the api and worker are replaced. Running jobs are interrupted by the restart and resume from their current step. The two exceptions are a live recording, which misses the segments that aired during the restart, and an in-flight YouTube upload, which starts that part again from zero. Check `GET /admin/jobs?state=running` first.

Migrations must stay **additive**: the old containers keep running against the migrated schema until the new ones start, and a code rollback leaves the schema migrated.

**Backups:** `docker compose --profile tools run --rm backup` writes a `pg_dump -Fc` archive into `ARCHIVE_BACKUP_DIR` (default `./backups`) and keeps the newest seven. Run it from a timer, and copy the dumps off the host. Restore one with [Restoring a dump, or moving from a native PostgreSQL](#restoring-a-dump-or-moving-from-a-native-postgresql)'s `pg_restore` line.

## Restoring a dump, or moving from a native PostgreSQL

Until this release the stack expected PostgreSQL on the host. To move an existing database into `db`:

```bash
docker compose stop api worker                       # nothing may write during the dump
sudo -u postgres pg_dump -Fc archive > archive.dump  # on the old server
# secrets/: add postgres_password, and change both URLs' host to db (new passwords are fine)
docker compose up -d --wait db
docker compose exec -T db pg_restore -U postgres -d archive --no-owner --no-privileges < archive.dump
docker compose up -d --wait                          # migrate, roles (grants), api, worker
```

`--no-privileges` skips the dump's grants, because the roles don't exist yet when it runs; the `roles` step recreates them and their grants. Compare row counts between the two servers before you switch the old one off, and keep it, stopped, until you're confident: switching back is stopping the stack and pointing the URLs at it again, losing what was written since.

Pushes and pull requests run CI (`.github/workflows/ci.yml`): lint (ruff, mypy), the unit tests and both image builds. Pushes to `dev` and `main` and `vX.Y.Z` tags also publish the images to GHCR. Changes merge into `dev` and reach production in a release from `dev` into `main` (docs/adr/0001). The database tests run there against an empty, migrated Postgres; the contract tests skip, because they need a restored copy of the real database: run them locally ([Development](development.md)) before merging anything that touches queries or response shapes.
