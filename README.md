# MirrorHub

MirrorHub configures and runs offline APT and Arch package mirrors and prepares
full or incremental packages for transfer through an external data diode tool.

## Release status

Development implementation. Not approved for production.

Passing unit tests alone does not establish mirror correctness, export
consistency, secure deployment, or compliance with all project requirements.

Review docs/release-checklist.md before deployment.

## Stack

- Python 3.12.
- FastAPI, SQLModel and SQLite.
- Jinja2 and vendored HTMX.
- Docker Compose.
- Caddy internal TLS.
- A universal worker for debmirror, rsync, wget and ftpsync.
- A separate export worker.
- No Redis, Node build process or Docker socket.

## Prerequisites

- A single Linux Docker host.
- Docker Engine and Docker Compose.
- Python 3.12 for local checks; Python 3.10 or newer on the Docker host for the
  smoke test.
- Local project directory and local Docker named volumes.
- Mirror and export directories provisioned for the selected UID/GID.
- DNS or client host entries for the configured HTTPS hostname.
- Internet access during dependency resolution and image builds, or suitable
  internal package registries and mirrors.

Runtime operation does not require a CDN.

## Configuration

Copy .env.example to .env and edit it. Every container reads .env; Compose also
uses it for variable substitution.

### Required for every deployment

| Variable | Default | Purpose |
|---|---|---|
| PUID, PGID | 1000 | Non-root numeric IDs of all containers. Must be able to write the NFS directories. Rebuild images after changing them. |
| HOST_DATA_DIR | /mnt/repository | Host path of the mirror tree, seen as /data in the containers. |
| HOST_EXPORT_DIR | /mnt/repository/export | Host path of the drop folder, seen as /export. |
| MIRRORHUB_HOSTNAME | localhost | Name clients use. Caddy serves exactly this name and issues its internal-CA certificate for it. |
| HTTPS_PORT | 8443 | Published host port. The container port is always 8443. |
| MIRRORHUB_PUBLIC_ORIGIN | https://localhost:8443 | Exact origin in the browser address bar: `https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT>`. Used for CSRF origin checks; a mismatch rejects the login and every form with 403. |
| MIRRORHUB_ADMIN_PASSWORD | empty | Initial admin password. Empty: a one-time password is printed to the first web log. Does not reset an existing account. |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| MIRRORHUB_ADMIN_USERNAME | admin | Initial admin username. |
| MIRRORHUB_SECRET_KEY | generated | Encrypts notification secrets. Empty: generated once and stored on the state volume. Back it up with the database. |
| MIRRORHUB_LOG_FORMAT | text | `text` or `json`. |
| MIRRORHUB_LOG_LEVEL | INFO | Python log level. |
| MIRRORHUB_WORKER_REPLICAS | 3 | Number of universal sync worker containers. |
| MIRRORHUB_WORKER_CONCURRENCY | 1 | Sync jobs per worker container. |
| MIRRORHUB_GLOBAL_CONCURRENCY | 2 | Initial global limit of parallel jobs; later changed on the Settings page. |
| MIRRORHUB_HEARTBEAT_INTERVAL_S | 10 | Initial worker heartbeat interval; Settings page afterwards. |
| MIRRORHUB_HEARTBEAT_STALE_S | 60 | Initial stale-job threshold; Settings page afterwards. |
| MIRRORHUB_LOG_RETENTION_DAYS | 30 | Initial job-log retention; Settings page afterwards. |
| MIRRORHUB_SESSION_TTL_S | 28800 | Login session lifetime (minimum 300). |
| MIRRORHUB_LOGIN_MAX_ATTEMPTS | 5 | Failed logins per username and client IP before a lockout. |
| MIRRORHUB_LOGIN_LOCKOUT_S | 300 | First lockout; doubles with each further failure, capped at one hour. |
| MIRRORHUB_MAX_GPG_UPLOAD_BYTES | 2097152 | Maximum size of an uploaded key file. |
| MIRRORHUB_TAG | dev | Image tag. |

### Proxy network

Caddy and the web container share a private network. Change these only if
172.31.254.0/28 overlaps a network the Docker host can reach, and then change all
three together:

| Variable | Default | Rule |
|---|---|---|
| MIRRORHUB_FRONTEND_SUBNET | 172.31.254.0/28 | Free private subnet. |
| MIRRORHUB_FRONTEND_IP_RANGE | 172.31.254.8/29 | Inside the subnet; addresses for other containers. |
| MIRRORHUB_PROXY_IP | 172.31.254.2 | Inside the subnet, outside the IP range, not the gateway (.1). |

The web container trusts X-Forwarded-For only from MIRRORHUB_PROXY_IP. Changing
these values requires `docker compose down` before `docker compose up -d`,
because Docker cannot change an existing network.

### Storage rules

HOST_DATA_DIR and HOST_EXPORT_DIR are host-side mount sources. Create them before
starting Compose, and arrange ownership and permissions on the NFS server when
NFS is used.

Do not put SQLite, its WAL files, the project directory or local worker guard
files on NFS. They live on the local `state` volume, together with sync
inventories, the hash cache and export metadata copies.

The repository ships docker-compose.override.yml, which Compose loads
automatically. It builds the proxy image with your PUID/PGID and supplies the
MIRRORHUB_PUBLIC_ORIGIN default.

## Dependencies and lockfiles

Install uv in a Python 3.12 tooling environment:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install uv
```

Resolve and export dependencies:

```sh
.venv/bin/python -m uv lock
.venv/bin/python -m uv export \
  --locked \
  --no-dev \
  --no-emit-project \
  --format requirements-txt \
  --output-file requirements.txt
```

Commit uv.lock and the generated requirements.txt.

Do not use a comment-only requirements.txt. Do not fabricate hashes or replace
the hashed export with pip freeze.

The Dockerfile installs the runtime requirements with --require-hashes.

Install the locked development environment:

```sh
.venv/bin/python -m uv sync --locked
```

Review resolved dependency versions and vulnerabilities before release.

Base images and operating-system packages also need a controlled update policy;
the Python lockfile does not pin them.

## Vendored browser assets

Run the existing vendor script on a connected machine:

```sh
sh scripts/vendor_htmx.sh
```

Commit the resulting static file.

The current vendor script prints a hash but does not enforce an independently
recorded expected hash. Resolve that release-checklist item before production.

## First deployment

Run all commands on the Docker host in the project directory.

1. Create the host directories and give them to PUID:PGID. On NFS with
   root_squash, set the ownership on the NFS server instead:

   ```sh
   sudo mkdir -p /mnt/repository/export
   sudo chown -R 1000:1000 /mnt/repository
   ```

2. Create and edit .env (see Configuration):

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

3. Check that the proxy subnet is free. 172.31.254.x must not appear:

   ```sh
   docker network ls -q | xargs docker network inspect -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'
   ip -4 route
   ```

4. Validate, build and start:

   ```sh
   docker compose config --quiet
   docker compose build
   docker compose up -d
   docker compose ps
   ```

   All services must become `healthy`; init-volumes ends as `exited (0)`.

   The web process runs the database migrations before starting. Workers wait
   for the schema. The web process is single-instance because it owns the
   scheduler.

5. Check the database revision. It must equal the newest file in
   migrations/versions:

   ```sh
   docker compose exec web python -c "import sqlite3; print(sqlite3.connect('/var/lib/mirrorhub/db/mirrorhub.sqlite3').execute('select version_num from alembic_version').fetchone()[0])"
   ls migrations/versions
   ```

6. Export the internal CA certificate and install it on the clients (see TLS
   trust):

   ```sh
   make root-cert
   ```

7. Open MIRRORHUB_PUBLIC_ORIGIN in a browser and log in (see Initial login).

8. Import the archive keys on the Keys page, then create the first repository.

9. Run the smoke test (see Testing):

   ```sh
   MIRRORHUB_URL=https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT> sh scripts/smoke_test.sh
   ```

## Upgrading

1. Read the commit log for new .env variables and migrations:

   ```sh
   git fetch origin
   git log --oneline HEAD..origin/master
   git diff HEAD..origin/master -- .env.example migrations/versions
   ```

2. Back up the database (see Backup).

3. Update the code and add new variables from .env.example to .env (the
   comparison needs bash):

   ```sh
   git switch master
   git pull
   diff <(grep -o '^[A-Z_]*=' .env.example | sort) <(grep -o '^[A-Z_]*=' .env | sort)
   ```

   Lines starting with `<` exist only in .env.example. Their defaults apply
   unless you add them to .env.

4. Rebuild and restart. Use `docker compose down` first when the proxy network
   changed (new or changed MIRRORHUB_FRONTEND_* or MIRRORHUB_PROXY_IP values, or
   an upgrade that introduces them). Volumes are kept.

   ```sh
   docker compose down                     # only when the network changed
   docker compose build
   docker compose up -d --remove-orphans
   docker compose ps
   ```

   `--remove-orphans` removes containers of services that no longer exist, for
   example the old per-tool workers.

5. Check the database revision (First deployment, step 5) and run the smoke
   test.

## Initial login

The default administrator username is admin (MIRRORHUB_ADMIN_USERNAME).

Set MIRRORHUB_ADMIN_PASSWORD before first start, or retrieve the one-time
generated password from the first web startup log.

The bootstrap environment variable does not reset an existing account.

Protect logs containing generated bootstrap credentials.

## TLS trust

Export the internal CA certificate:

```sh
make root-cert
```

This copies proxy:/data/caddy/pki/authorities/local/root.crt to
./caddy-root.crt.

Install it into the trust stores of authorized clients through your organization's
normal certificate distribution process.

For command-line diagnostics, prefer explicitly trusting that certificate over
disabling certificate verification.

Changing the Caddy CA volume changes the CA identity. Back up that volume.

## Login and proxy security

- Failed logins are counted per username and client IP. After
  MIRRORHUB_LOGIN_MAX_ATTEMPTS failures that address is locked for
  MIRRORHUB_LOGIN_LOCKOUT_S, doubling with every further failure up to one hour.
  Other addresses can still log in, so an attacker cannot lock the administrator
  out. Counters reset after 24 hours without failures.
- An attacker with many source addresses still gets that many guesses per
  address. Use a strong administrator password.
- The client IP comes from X-Forwarded-For, trusted only from Caddy
  (MIRRORHUB_PROXY_IP). Browsers on the Docker host itself appear as the
  network gateway, for example 172.31.254.1; remote clients appear with their
  own address.

## UID/GID and NFS

The runtime images create the mirror user with the build-time PUID/PGID.

Rebuild images after changing PUID/PGID.

Pre-provision NFS directories so those numeric IDs can write. NFS root_squash
does not grant the application permission to fix server-side ownership.

The current init-volumes service is a root initialization exception that changes
ownership only on local named volumes. Runtime web, workers and proxy use the
configured non-root identity.

If the requirement is strictly that every service must run non-root, replace
this initialization approach before deployment.

## Alternative NFS named volumes

The supplied Compose file documents an alternative to host-mounted NFS bind
sources.

Example volume definition:

```yaml
volumes:
  mirror_data:
    driver: local
    driver_opts:
      type: nfs
      o: "addr=nas.example.lan,nfsvers=4.1,rw,hard"
      device: ":/export/mirrors"
```

Apply the same pattern to export_dir.

Do not apply it to the SQLite state or GPG volumes.

Test NFS permissions, rename behavior and fsync support against the actual
server before relying on export publication.

## Sync architecture

A sync job is first converted into a typed `SyncPlan`. The plan records the job,
repository, upstream, target paths and tool-specific configuration.

Each supported tool has an adapter:

- `FtpsyncAdapter` for Debian-style FTPsync mirrors.
- `RsyncAdapter` for generic and Arch rsync mirrors.
- `DebmirrorAdapter` for APT repositories synchronized with debmirror.
- `WgetAdapter` for HTTP directory trees and extra-file downloads.

The adapter validates its plan, delegates to the tool-specific builder and
interprets the runner result. The shared `SyncAdapter` base class handles common
success, failure and cancellation interpretation.

The worker executes sync jobs only through these adapters. Tool-specific builders
remain responsible for command construction, validation and tool-specific safety
rules.

## Repository setup

Create a repository directly or clone an editable preset.

For APT, configure suites, components and architectures explicitly.

Import the relevant archive verification keys through the Keys page.

Keep signature checking enabled unless a documented exception is necessary.
Disabling it requires explicit confirmation and produces an audit entry.

For generic wget repositories, provide URL-to-relative-path entries.

The web data mount is read-only. Workers write mirror data.

Review the current builder limitations in docs/release-checklist.md before
starting large or production mirrors.

## Worker scaling

The `worker` service is a universal sync worker. It handles FTPsync, rsync,
debmirror and wget jobs.

Set the number of worker containers in `.env`:

```sh
MIRRORHUB_WORKER_REPLICAS=3
```

Apply the change with:

```sh
docker compose up -d
```

Alternatively, override the replica count for one deployment:

```sh
docker compose up -d --scale worker=3
```

Each worker can run multiple jobs concurrently:

```sh
MIRRORHUB_WORKER_CONCURRENCY=2
```

For example, three workers with a concurrency of two can run up to six sync
jobs in parallel, subject to the configured global concurrency limit and
per-repository locks.

The SQLite queue enforces the configured global concurrency limit.

Repository guards on the local state volume protect execution against overlap
with a stale process. A repository is never synchronized concurrently by two
jobs.

Do not scale web or distribute these services across multiple Docker hosts.

## Jobs and schedules

Jobs support manual start, dry run and cancellation.

Cron expressions use five fields and UTC.

The scheduler runs in web. A disabled repository is skipped by scheduled syncs.

Stopping a subprocess sends SIGTERM, followed by SIGKILL after its grace period.

Exports cancel cooperatively until publication begins.

Job history is retained independently of log-file retention.

"Export after a successful sync" can be set on the repository and on each
schedule:

- Repository checkbox: applies to manual syncs and to schedules set to "Use the
  repository setting" (the default for new schedules).
- Schedule "Always" or "Never": overrides the repository for syncs started by
  that schedule.

Only a sync that chains an export writes a sync inventory; it is deleted after
the export succeeded. Leftovers older than seven days are removed automatically.

## Notifications

1. Create an SMTP or webhook channel.
2. Send a channel test.
3. Create event rules for the events that should use the channel.

A channel without rules does not receive job events.

Secrets are encrypted using the application secret. Back up that secret together
with the database.

The current notification high-water mark can miss jobs that finish out of job-ID
order. This is a release blocker, not a documented reliability guarantee.

## Data diode exports

Read docs/export-contract.md.

The pickup tool must consume only export directories with .done. It may move
them out of the drop folder and delete them later; moving the whole
`<slug>/<export-id>/` directory at once is preferred.

Incremental exports use complete manifests and SHA-256 comparisons, not a
timestamp-only filter.

What MirrorHub keeps on the local state volume:

- `export-meta/<slug>/<export-id>/`: files.json, manifest.json,
  MANIFEST.sha256(.sig) and deleted.txt of each export, written before .done.
  The next incremental export reads its base from here and does not need or
  hash the previous archive. The newest 30 copies per repository and the newest
  published one are kept.
- `manifests/hashcache-<slug>.json`: SHA-256 cache for unchanged files (same
  inode, size, mtime and ctime). Incremental exports use it; full exports
  rehash every file and rebuild it.

Automatic cleanup in the drop folder (scheduler, about every 30 seconds):

- Empty export directories.
- Export directories holding only metadata files and .done, no archive part,
  unchanged for 24 hours.
- Empty repository directories without a running export.

`.tmp` directories and anything unexpected are never touched. The Exports page
shows for each export whether it is `present`, `partial` or `picked up`.

Exports that still contain archive parts are deleted only by the retention rule
on the Exports page, which also removes their metadata copies.

If the base of an incremental export is gone (no metadata copy and no export
directory), the export fails with "run a full export". Run a full export and
import it on the receiver with `--rebase`.

Verify an export:

```sh
docker compose exec worker-export python -m mirrorhub.verify /export/<slug>/<export-id>
```

The verifier uses only the standard library. It validates package checksums and
metadata, not GPG signature authenticity or receiver-side chain application.

Provision signature trust separately.

## Backup

Do not copy only the live mirrorhub.sqlite3 file while WAL activity continues.

Create a consistent backup inside the local state volume:

```sh
docker compose exec web python -m mirrorhub.backup \
  /var/lib/mirrorhub/db/mirrorhub.sqlite3 \
  /var/lib/mirrorhub/backups/mirrorhub-backup.sqlite3
```

The destination must not already exist.

Copy it to protected backup storage:

```sh
mkdir -p backups
chmod 700 backups
docker compose cp \
  web:/var/lib/mirrorhub/backups/mirrorhub-backup.sqlite3 \
  ./backups/mirrorhub-backup.sqlite3
```

Also preserve:

- The generated state-volume secret_key, or the externally supplied secret.
- The GPG volume, including any configured signing key.
- The Caddy data volume containing the internal CA.
- .env, uv.lock, requirements.txt and deployment configuration.
- Mirror data according to your retention policy.

The state volume also holds export metadata copies (export-meta/) and the hash
cache. Back up export-meta/ together with the database, otherwise the next
incremental export after a restore needs a full export first.

Database and filesystem backups taken at different times are not automatically
a consistent application snapshot. For a coordinated recovery point, stop jobs
and services before taking the complete volume/filesystem backup.

Keep signing keys and application secrets encrypted in backup storage.

## Restore

1. Stop the deployment.
2. Restore the database into the local state volume.
3. Restore the matching application secret and GPG material.
4. Restore compatible mirror and export filesystem state.
5. Restore numeric ownership for PUID/PGID.
6. Start the deployment and inspect stale-job recovery.
7. Verify export bases before creating incremental exports.

Do not combine a database backup with unrelated export directories and assume
the incremental chain remains valid.

## Testing

### Local checks

```sh
sh scripts/check.sh
```

Runs compileall, ruff check, ruff format --check, mypy (strict), pytest and
`docker compose config`. The checks stop at the first failure. A mypy failure is
a failed release gate; do not disable strict mode to hide it.

GitHub Actions runs the same gates for every pull request and push to master
(.github/workflows/check.yml), and validates the Caddyfile with the caddy image.

### Docker smoke test

Start the deployment first, then run:

```sh
MIRRORHUB_URL=https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT> sh scripts/smoke_test.sh
```

It needs Python 3.10 or newer on the host and no admin password. It checks:

- The services web, worker, worker-export and proxy are running.
- HTTPS against the Caddy internal CA and the login page.
- Unauthenticated requests are redirected to the login page.
- debmirror, gpgv, patch and ed in the worker, and public keys in
  /gnupg/trustedkeys.gpg.
- Free space on the mirror data mount (default: at least 10 %).

Extra arguments are passed to scripts/smoke_test.py, for example --json.

It does not sync a repository, publish an export, send a notification, or prove
upstream mirror correctness.

### Acceptance checks after deployment or upgrade

Container health and restarts:

```sh
docker inspect -f '{{.Name}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{end}}' $(docker compose ps -q)
docker compose logs --since 15m | grep -i -E "error|traceback|exception" || echo "no errors"
```

Proxy trust. The first command prints MIRRORHUB_PROXY_IP; the second must show
the proxy container at that address:

```sh
docker compose exec web printenv FORWARDED_ALLOW_IPS
docker network inspect mirrorhub_frontend -f '{{range .Containers}}{{.Name}}={{.IPv4Address}} {{end}}'
```

After a login, the Audit log page must show the client's real IP (or the
gateway address for a browser on the Docker host).

Signature keys for debmirror in the sync worker:

```sh
docker compose exec worker gpg --no-default-keyring --keyring /gnupg/trustedkeys.gpg --list-keys | grep -c '^pub'
```

Then run a small debmirror sync and follow `docker compose logs -f worker`.

Export chain with a moved export. Use a repository whose receiver side may get a
fresh full export afterwards:

```sh
EXPORT_DIR=$(grep '^HOST_EXPORT_DIR=' .env | cut -d= -f2)
SLUG=<slug>
# 1. Start a full export on the Exports page, then:
docker compose exec web ls /var/lib/mirrorhub/export-meta/$SLUG/
# 2. Simulate the pickup tool:
FULL=$(ls "$EXPORT_DIR/$SLUG" | grep -- '-full$' | tail -1)
mkdir -p /tmp/pickup-test && sudo mv "$EXPORT_DIR/$SLUG/$FULL" /tmp/pickup-test/
# 3. Start an incremental export on the Exports page, then:
INCR=$(ls "$EXPORT_DIR/$SLUG" | grep -- '-incr$' | tail -1)
docker compose exec worker-export python -m mirrorhub.verify /export/$SLUG/$INCR
```

The incremental export must succeed with base_export_id equal to $FULL, and the
Exports page must show the full export as `picked up`.

Drop-folder cleanup. The directory must disappear within about a minute, and
the Audit log shows `exports.drop_folder_cleanup`:

```sh
sudo mkdir "$EXPORT_DIR/$SLUG/20260101T000000Z-incr"
sleep 60; ls "$EXPORT_DIR/$SLUG/"
```

Login lockout (optional): five wrong passwords from client A lock only A; a
correct login from client B must still work.

Login/logout and rejected requests may create audit records.

## Troubleshooting

### Package has no installation candidate

Inspect the full apt build output. Do not interpret every apt exit code 100 as
a network failure.

Ubuntu archive keys can be uploaded; ubuntu-keyring is not guaranteed to be
available in the Debian image's package sources.

### Import errors or missing module functions

Check exact filenames and whether the file was saved completely.

Package initialization files are named __init__.py, with two underscores on
each side.

Use:

```sh
python -m compileall -q src
python -m pytest
```

### Syntax error at the end of a file

A bare if or incomplete expression indicates an incomplete source file.
Replace the complete file, then run py_compile before pytest.

### NFS permission denied

Check server-side numeric UID/GID ownership and export permissions.
Do not disable root_squash as a substitute for correct ownership.

### Export checksum verification fails

Do not weaken the verifier or modify an already published export.
Preserve it for inspection and generate a new full export after correcting the
writer.

### Missing .done

The export is not published and must not be consumed, even if its final directory
exists.

### Incremental export fails with "run a full export"

Neither the metadata copy on the state volume nor the previous export directory
exists, for example after restoring the database without export-meta/. Start a
full export and import it on the receiver with --rebase.

### Login or forms fail with 403 "CSRF validation failed"

MIRRORHUB_PUBLIC_ORIGIN does not match the address in the browser. The web log
shows `CSRF rejected: reason=...`. Set it to
`https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT>` exactly and run
`docker compose up -d`.

### "Pool overlaps with other one on this address space"

The proxy subnet collides with an existing network. Change
MIRRORHUB_FRONTEND_SUBNET, MIRRORHUB_FRONTEND_IP_RANGE and MIRRORHUB_PROXY_IP
together (see Configuration), then `docker compose down` and
`docker compose up -d`.

### Image build cannot reach docker.io

`docker compose build` needs Docker Hub for the base images unless they are
cached. Do not use `--pull` on hosts without registry access. Check with
`docker pull docker.io/library/caddy:2-alpine`.

### Caddy logs "aborting with incomplete response" for /jobs/<id>/events

The browser closed the live job-progress stream, for example by leaving the
page. This is harmless.

### SQLite locked

Confirm SQLite is local, web is single-instance, and no long-running transaction
is held across network or filesystem work.

Do not increase concurrency or busy_timeout without investigating the cause.

### Inspect FTPsync job logs

MirrorHub runs FTPsync jobs in the universal `worker` service. The job-specific
working directory and rsync log are created under the worker state volume.

For a repository with the slug `packages.linuxmint.com`, inspect the generated
FTPsync log with:

```sh
docker compose exec worker cat \
  /var/lib/mirrorhub/tmp/ftpsync/packages.linuxmint.com/log/rsync-ftpsync-packages.linuxmint.com.log
```

To follow the log while a job is running:

```sh
docker compose exec worker tail -f \
  /var/lib/mirrorhub/tmp/ftpsync/packages.linuxmint.com/log/rsync-ftpsync-packages.linuxmint.com.log
```

To find the newest job log when the repository slug is unknown:

```sh
docker compose exec worker sh -c \
  'find /var/lib/mirrorhub/tmp/ftpsync -type f -name "*.log" -printf "%T@ %p\n" | sort -nr | head'
```

The universal worker also writes aggregate logs to stdout:

```sh
docker compose logs -f worker
```

Use the UI job history to identify the job ID before inspecting a specific log.

## Security boundary

MirrorHub runs package-transfer tools against administrator-configured upstreams.

Treat upstream metadata, downloaded packages, logs and uploaded key material as
untrusted input.

The single administrator is trusted to configure upstreams and notification
destinations.

A non-root container and a read-only root filesystem do not replace application
validation, filesystem containment checks or dependency maintenance.
