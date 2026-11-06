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
- debmirror, rsync and wget workers.
- A separate export worker.
- No Redis, Node build process or Docker socket.

## Prerequisites

- A single Linux Docker host.
- Docker Engine and Docker Compose.
- Python 3.12 for local checks.
- Local project directory and local Docker named volumes.
- Mirror and export directories provisioned for the selected UID/GID.
- DNS or client host entries for the configured HTTPS hostname.
- Internet access during dependency resolution and image builds, or suitable
  internal package registries and mirrors.

Runtime operation does not require a CDN.

## Configuration

Copy .env.example to .env and edit:

- PUID and PGID.
- MIRROR_DATA_DIR.
- EXPORT_DIR.
- MIRRORHUB_HOSTNAME.
- HTTPS_PORT.
- MIRRORHUB_ADMIN_PASSWORD.
- MIRRORHUB_SECRET_KEY, if externally managed.

Use non-root PUID and PGID values.

MIRROR_DATA_DIR and EXPORT_DIR are host-side mount sources. The application sees
them at /data and /export.

Create these directories on the host before starting Compose. Arrange ownership
and permissions on the NFS server when NFS is used.

Do not put SQLite, its WAL files, the project directory or local worker guard
files on NFS.

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

## Build and start

```sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

Open the configured HTTPS hostname and port.

The web process runs migrations before starting. Workers wait for the schema.

The web process is single-instance because it owns the scheduler.

## Initial login

The default administrator username is admin.

Set MIRRORHUB_ADMIN_PASSWORD before first start, or retrieve the one-time
generated password from the first web startup log.

The bootstrap environment variable does not reset an existing account.

Protect logs containing generated bootstrap credentials.

## TLS trust

Export the internal CA certificate:

```sh
docker compose cp \
  proxy:/data/caddy/pki/authorities/local/root.crt \
  ./caddy-root.crt
```

Install it into the trust stores of authorized clients through your organization's
normal certificate distribution process.

For command-line diagnostics, prefer explicitly trusting that certificate over
disabling certificate verification.

Changing the Caddy CA volume changes the CA identity. Back up that volume.

## UID/GID and NFS

The runtime images create the mirror user with the build-time PUID/PGID.

Rebuild images after changing PUID or PGID.

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

Scale workers by tool:

```sh
docker compose up -d --scale worker-debmirror=2
docker compose up -d --scale worker-rsync=2
docker compose up -d --scale worker-wget=2
docker compose up -d --scale worker-export=2
```

The SQLite queue enforces the configured global concurrency limit.

Repository guards on the local state volume protect execution against overlap
with a stale process.

Do not scale web or distribute these services across multiple Docker hosts.

## Jobs and schedules

Jobs support manual start, dry run and cancellation.

Cron expressions use five fields and UTC.

The scheduler runs in web. A disabled repository is skipped by scheduled syncs.

Stopping a subprocess sends SIGTERM, followed by SIGKILL after its grace period.

Exports cancel cooperatively until publication begins.

Job history is retained independently of log-file retention.

## Notifications

1. Create an SMTP or webhook channel.
2. Send a channel test.
3. Create event rules for the events that should use that channel.

A channel without rules does not receive job events.

Secrets are encrypted using the application secret. Back up that secret together
with the database.

The current notification high-water mark can miss jobs that finish out of job-ID
order. This is a release blocker, not a documented reliability guarantee.

## Data diode exports

Read docs/export-contract.md.

The pickup tool must consume only export directories with .done.

Incremental exports use complete manifests and SHA-256 comparisons, not a
timestamp-only filter.

MirrorHub never automatically deletes exports.

Verify an export:

```sh
python -m mirrorhub.verify /path/to/export-directory
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
- Mirror data and export inventories according to your retention policy.

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

## Local checks

```sh
sh scripts/check.sh
```

The checks stop at the first failure. A mypy failure is a failed release gate;
do not disable strict mode to hide it.

## Docker smoke test

Start the deployment first, then run:

```sh
MIRRORHUB_URL=[https://mirrorhub.lan:8443](https://mirrorhub.lan:8443) \
  sh scripts/smoke_test.sh
```

The script prompts for the admin password unless MIRRORHUB_SMOKE_PASSWORD is set.

It checks:

- Expected services and container health.
- HTTPS with the internal CA.
- Login and authenticated pages.
- Secure session-cookie presence.
- CSRF rejection and logout.

It does not sync a repository, publish an export, send a notification, or prove
upstream mirror correctness.

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

### SQLite locked

Confirm SQLite is local, web is single-instance, and no long-running transaction
is held across network or filesystem work.

Do not increase concurrency or busy_timeout without investigating the cause.

## Security boundary

MirrorHub runs package-transfer tools against administrator-configured upstreams.

Treat upstream metadata, downloaded packages, logs and uploaded key material as
untrusted input.

The single administrator is trusted to configure upstreams and notification
destinations.

A non-root container and a read-only root filesystem do not replace application
validation, filesystem containment checks or dependency maintenance.