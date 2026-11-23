# MirrorHub – Operations Handbook

As of 23 Nov 2026 (PR #51). Repository: https://github.com/fr4kt0s/mirrorhub. This handbook lives in the repository (`docs/operations-handbook.md`) and describes the code of the same commit.

MirrorHub has two parts:

- **Sender** (Docker host with internet access): synchronises package mirrors and writes exports to the drop folder. A pickup tool carries them across the data diode.
- **Receiver** (behind the diode): the importer (`mirrorhub-importer.pyz`) applies the exports to a local mirror tree.

Sender and importer must be built from the same commit. The sender shows its commit at the bottom of every page, the importer with `--version`.

---

## 1. First deployment

### 1.1 Sender (Docker host)

Run all commands in the project directory on the Docker host.

1. Create the host directories and hand them to PUID:PGID (with NFS root_squash, set the ownership on the NFS server):

   ```sh
   sudo mkdir -p /mnt/repository/export
   sudo chown -R 1000:1000 /mnt/repository
   ```

2. Create and fill in `.env`:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   Required values:

   | Variable | Meaning |
   | --- | --- |
   | `PUID`, `PGID` | non-root IDs of all containers; must be able to write the NFS directories |
   | `HOST_DATA_DIR` | mirror tree on the host (`/data` in the container) |
   | `HOST_EXPORT_DIR` | drop folder on the host (`/export` in the container) |
   | `MIRRORHUB_HOSTNAME` | name clients use to open the web interface |
   | `HTTPS_PORT` | published port (default 8443) |
   | `MIRRORHUB_PUBLIC_ORIGIN` | exactly `https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT>`, otherwise every login fails with 403 |
   | `MIRRORHUB_ADMIN_PASSWORD` | initial password; empty = one-time password in the first web log |

   Optional, new: `MIRRORHUB_EXPORT_META_KEEP` (default 30) – how many export metadata copies are kept per repository; each one can be the base for "Incremental since".

3. Check that the proxy subnet 172.31.254.x is free:

   ```sh
   docker network ls -q | xargs docker network inspect -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'
   ip -4 route
   ```

4. Build and start:

   ```sh
   docker compose config --quiet
   docker compose build
   docker compose up -d
   docker compose ps          # all healthy, init-volumes "exited (0)"
   ```

5. Check the database revision (must match the newest file in `migrations/versions`, currently `0009`):

   ```sh
   docker compose exec web python -c "import sqlite3; print(sqlite3.connect('/var/lib/mirrorhub/db/mirrorhub.sqlite3').execute('select version_num from alembic_version').fetchone()[0])"
   ```

6. Export the CA certificate and install it on the clients: `make root-cert`
7. Open `MIRRORHUB_PUBLIC_ORIGIN` in the browser and log in.
8. On the *Keys* page, import the archive keys, then create the first repository.
9. Smoke test (8/8 expected):

   ```sh
   MIRRORHUB_URL=https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT> sh scripts/smoke_test.sh
   ```

10. Build the importer for the receiver (from the same checkout):

    ```sh
    make importer
    # -> dist/mirrorhub-importer.pyz and dist/mirrorhub-importer.pyz.sha256
    ```

### 1.2 Receiver

Requirements:

- Python 3.12 or newer (`python3 --version`)
- `zstd`, if a repository exports with archive format `tar.zst` (the default is `tar`)
- `gpgv`, if exports are signed and the signature should be checked

No database, web application or additional Python packages are needed.

1. Create the directories (example from `importer.example.toml`):

   ```sh
   sudo mkdir -p /srv/mirrorhub/inbox /srv/mirrorhub/mirror /var/lib/mirrorhub-importer /opt/mirrorhub /etc/mirrorhub
   ```

   - `inbox`: the pickup tool places `<slug>/<export-id>/` here.
   - `mirror`: one tree per repository, `mirror/<slug>/`, served by the web server.
   - State directory: **outside** the mirror tree.
   - Staging (default `mirror/.mirrorhub-import`) must be on the same file system as `mirror`.

2. Configuration `/etc/mirrorhub/importer.toml`:

   ```toml
   export_dir = "/srv/mirrorhub/inbox"
   mirror_root = "/srv/mirrorhub/mirror"
   state_dir = "/var/lib/mirrorhub-importer"
   # keyring = "/etc/mirrorhub/export-signers.gpg"
   # require_signature = false
   ```

3. Transfer and verify the importer:

   ```sh
   cd /opt/mirrorhub
   sha256sum -c mirrorhub-importer.pyz.sha256
   python3 mirrorhub-importer.pyz --version     # commit must match the sender's footer
   ```

4. Apply the first export (the first export of a repository is always a full export):

   ```sh
   python3 mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml import --slug <slug>
   python3 mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml status
   ```

   If the target tree already contains data, a full export is only allowed with `--rebase`. If the existing tree matches an export in the inbox exactly, `adopt --slug <slug> --auto` is enough instead (no transfer).

5. Check the tree and create the report directory:

   ```sh
   sudo mkdir -p /var/lib/mirrorhub-reports
   python3 mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml check --all --output-dir /var/lib/mirrorhub-reports
   ```

   Expected: exit code 0, all lists in `/var/lib/mirrorhub-reports/<slug>/` empty.

6. Periodic check with a systemd timer:

   ```ini
   # /etc/systemd/system/mirrorhub-check.service
   [Service]
   Type=oneshot
   ExecStart=/usr/bin/python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml check --all --output-dir /var/lib/mirrorhub-reports

   # /etc/systemd/system/mirrorhub-check.timer
   [Timer]
   OnCalendar=weekly
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now mirrorhub-check.timer
   ```

   Exit code 11 means the tree differs. Add the service state (failed) to monitoring. Also run an occasional check with `--hash` (reads every file, finds silent corruption).

---

## 2. Updating an existing system

Always back up first (step 1); it is required before migrations that rebuild tables (`0006`, `0008`). Step 2 shows whether the update brings new migrations or `.env` variables.

### 2.1 Sender

1. Back up the database and the export metadata, **outside** the project directory:

   ```sh
   D=$(date +%Y%m%d)
   docker compose exec web python -m mirrorhub.backup /var/lib/mirrorhub/db/mirrorhub.sqlite3 /var/lib/mirrorhub/backups/mirrorhub-$D.sqlite3
   mkdir -p ~/mirrorhub-backups
   docker compose cp web:/var/lib/mirrorhub/backups/mirrorhub-$D.sqlite3 ~/mirrorhub-backups/
   docker compose cp web:/var/lib/mirrorhub/export-meta ~/mirrorhub-backups/export-meta-$D
   ```

2. Review new variables and migrations, then update:

   ```sh
   git fetch origin
   git log --oneline HEAD..origin/master
   git diff HEAD..origin/master -- .env.example migrations/versions
   git switch master && git pull
   diff <(grep -o '^[A-Z_]*=' .env.example | sort) <(grep -o '^[A-Z_]*=' .env | sort)
   ```

3. Build and start:

   ```sh
   docker compose build
   docker compose up -d --remove-orphans
   docker compose ps
   MIRRORHUB_URL=https://<MIRRORHUB_HOSTNAME>:<HTTPS_PORT> sh scripts/smoke_test.sh
   ```

4. Rebuild the importer: `make importer`

### 2.2 Receiver

Replace `mirrorhub-importer.pyz` and `.sha256`, run `sha256sum -c`, compare `--version` with the sender's footer. The state files are kept; on the next import or `check`, the new importer creates the inventory copy under `<state_dir>/inventories/`.

---

## 3. Pending tests

3.1–3.4 are concrete practical tests with commands; 3.5 is the full checklist.

### 3.1 After the update (#44–#49)

- [ ] The footer shows the commit; the containers report the same as `git rev-parse --short=12 HEAD`:

  ```sh
  git rev-parse --short=12 HEAD
  docker compose exec web python -c "from mirrorhub import buildinfo; print(buildinfo.describe())"
  ```

- [ ] The audit log shows `exports.reconcile`; every published metadata copy has a marker (both numbers equal; directories without a marker belong to exports that were never published):

  ```sh
  docker compose exec web sh -c 'ls -d /var/lib/mirrorhub/export-meta/*/*/ | grep -vc "\.tmp/$"'
  docker compose exec web sh -c 'ls /var/lib/mirrorhub/export-meta/*/*/.published | wc -l'
  ```

- [ ] The Exports page shows "Incremental since …" for each repository.
- [ ] Receiver: `--version` = sender commit; `check --all --output-dir …` ends with 0, all lists empty.
- [ ] A normal import runs through; `status` shows the new state.

### 3.2 Practical test "detection" (receiver)

- [ ] Rename an unimportant file, check, rename it back:

  ```sh
  M=/srv/mirrorhub/mirror/<slug>
  mv $M/<path> $M/<path>.moved
  python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml check --slug <slug> --output-dir /tmp/mh-check; echo "exit $?"
  cat /tmp/mh-check/<slug>/missing.txt /tmp/mh-check/<slug>/extra.txt
  mv $M/<path>.moved $M/<path>
  ```

  Expected: exit 11, `missing.txt` contains `<path>`, `extra.txt` contains `<path>.moved`; after renaming back, exit 0.

### 3.3 Practical test "lost export / incremental since"

Starting point: the receiver is current, `status` shows the newest export as `last` (E0).

- [ ] Sender: start an incremental export (E1).
- [ ] Receiver: move E1 out of the inbox before importing (simulated loss).
- [ ] Sender: Exports page → "Incremental since …" → choose E0 (E2).
- [ ] Receiver: `import --slug <slug>` applies E2, `status` shows `last=E2`.
- [ ] Receiver: put E1 back into the inbox, `import --slug <slug>` → E1 is reported as `superseded`, exit 0, `status` shows `superseded=1`. On the sender, E1 is marked "superseded".

### 3.4 Optional: `adopt` (receiver)

- [ ] Set the state file aside, run `adopt`, compare the result:

  ```sh
  S=/var/lib/mirrorhub-importer
  cp $S/<slug>.json /tmp/<slug>.json.bak && mv $S/<slug>.json /tmp/<slug>.json.moved
  python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml adopt --slug <slug> --auto
  python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml status --slug <slug>
  ```

  Expected: `adopted <export-id>` with the same export that was `last` before. Afterwards restore the old file (`cp /tmp/<slug>.json.bak $S/<slug>.json`) so the history is kept.

### 3.5 Test checklist

**Legend**

- **P1**: before production use. **P2**: soon after. **P3**: when convenient.
- **[test system]**: changes or destroys data. Run only on a test system or with a throwaway repository.
- `MH` is the shorthand from 4.2. `<slug>` stands for a small test repository.
- Each test is followed by the expected result (→). If it differs, note the date, commit (`--version` or footer) and output.

The practical tests 3.1–3.4 are referenced here, not repeated. The P1 tests alone, as a printable sign-off sheet: [`acceptance-p1.md`](acceptance-p1.md).

#### A. Sender: deployment, login, security

- [ ] **A1** (P1) `docker compose ps` → all services `healthy`, `init-volumes` `exited (0)`.
- [ ] **A2** (P1) Database revision (1.1 step 5) → `0009`.
- [ ] **A3** (P1) Smoke test → 8/8.
- [ ] **A4** (P1) Commit in the footer and in `web`, `worker` and `worker-export` equals `git rev-parse --short=12 HEAD`:
  `for s in web worker worker-export; do docker compose exec $s python -c "from mirrorhub import buildinfo; print('$s', buildinfo.describe())"; done`
- [ ] **A5** (P1) CA certificate (`make root-cert`) installed on a client → browser opens the interface without a certificate warning.
- [ ] **A6** (P1) Log in with the admin account → dashboard; wrong password → error message, no login.
- [ ] **A7** (P1) Open the interface via another name or the IP instead of `MIRRORHUB_HOSTNAME` → login rejected with 403 (confirms `MIRRORHUB_PUBLIC_ORIGIN` is in effect).
- [ ] **A8** (P2) `MIRRORHUB_LOGIN_MAX_ATTEMPTS` (default 5) failed attempts → account locked for `MIRRORHUB_LOGIN_LOCKOUT_S` (default 300 s); further failures double the lockout up to 1 h; afterwards login works again. Use a test user or a test system.
- [ ] **A9** (P2) Lockout applies per user name and IP: from a second client, login still works during the lockout.
- [ ] **A10** (P2) Session expires after `MIRRORHUB_SESSION_TTL_S` → next click leads to the login page.
- [ ] **A11** (P2) Without login: every page redirects to the login page; `/config/export.yaml` returns no content.
- [ ] **A12** (P2) POST without CSRF token (e.g. `curl -X POST …/jobs/cleanup` with a valid session cookie but no token) → rejected, nothing changed.
- [ ] **A13** (P2) Audit log shows the real client IP, not the proxy address from 172.31.254.x.
- [ ] **A14** (P2) Browser console on every page (Dashboard, Repositories, Jobs, Exports, Schedules, Keys, Notifications, Settings, Storage, Config, Audit) → no CSP violations, all buttons work.
- [ ] **A15** (P2) `docker compose restart` → all `healthy` again, repositories, jobs and settings unchanged.
- [ ] **A16** (P2) Restart the worker during a sync (`docker compose restart worker`) → the job does not stay `running` forever (detected after `MIRRORHUB_HEARTBEAT_STALE_S` at the latest); a new sync runs cleanly, the log has a new `# run` header.
- [ ] **A17** (P3) Reboot the Docker host → the stack starts by itself (`restart: unless-stopped`), A1 holds.
- [ ] **A18** (P3) `docker compose logs --since 1h | grep -i -E "traceback|exception"` after a normal day of operation → empty.

#### B. Keys

- [ ] **B1** (P1) Upload a key file (`.asc`/`.gpg`) → appears with fingerprint and user ID.
- [ ] **B2** (P1) Import keys from a keyring package (import package) → the contained keys appear.
- [ ] **B3** (P1) Sync a signed debmirror repository with the matching key → succeeds.
- [ ] **B4** (P2) [test system] Sync without the matching key → error with a signature hint in the log, mirror tree not damaged.
- [ ] **B5** (P2) Upload a text file instead of a key → clear error message, no crash.
- [ ] **B6** (P3) File larger than `MIRRORHUB_MAX_GPG_UPLOAD_BYTES` → rejected.
- [ ] **B7** (P3) Upload the same key twice → no duplicate entry.
- [ ] **B8** (P2) [test system] Delete a key → disappears from the list; the next sync of a repository that needs it fails as in B4.

#### C. Repositories and sync

- [ ] **C1** (P1) Create and sync a debmirror repository → job `succeeded`, `/data/<slug>/` contains `dists/` and `pool/`.
- [ ] **C2** (P1) ftpsync with empty *Architectures to include* → sync starts, no configuration error (#41).
- [ ] **C3** (P1) ftpsync with include only (e.g. `amd64`) → only that architecture under `dists/*/binary-*` and in `pool`.
- [ ] **C4** (P2) ftpsync with exclude only → excluded architectures are missing.
- [ ] **C5** (P2) ftpsync with include and exclude at the same time → form error, nothing saved.
- [ ] **C6** (P2) ftpsync with `none` as architecture → form error.
- [ ] **C7** (P1) Log of an ftpsync job → no debmirror entries (original finding).
- [ ] **C8** (P2) rsync repository → sync succeeds, tree complete.
- [ ] **C9** (P2) wget repository, mode *Whole directory tree* → saves without a file list (#51), sync succeeds, tree below the upstream root complete.
- [ ] **C26** (P2) wget tree `download.docker.com` `/linux/ubuntu` with *Architectures to keep* `amd64` → `/data/<slug>/dists/*/pool/stable/` contains only `amd64`; `binary-arm64` etc. missing; edit page shows the generated regex; include/exclude regex survive editing.
- [ ] **C27** (P2) Preset *Docker CE (Ubuntu)* with the Docker key imported → debmirror sync (dry run, then real) succeeds.
- [ ] **C10** (P1) Configuration error (e.g. invalid option) → job `failed` at once, no retry, the message names the cause and does not contain "exit code None".
- [ ] **C11** (P2) Unreachable host → retries as configured, then `failed` and a `retry_exhausted` notification.
- [ ] **C12** (P2) Cancel a running sync → job `cancelled`, no sync process left (`docker compose exec worker ps aux`).
- [ ] **C13** (P2) Start a sync while the same repository's sync is queued or running → rejected ("already queued"), no second job.
- [ ] **C14** (P3) Start more repositories at once than `MIRRORHUB_GLOBAL_CONCURRENCY` / `MIRRORHUB_WORKER_CONCURRENCY` → at most that many run, the rest wait and start afterwards.
- [ ] **C15** (P2) Edit and save a repository → values persist after reload; the next sync uses them (visible in the log).
- [ ] **C16** (P2) [test system] Delete a repository → disappears from the list and the schedules; note what happens to `/data/<slug>/` and its exports.
- [ ] **C17** (P2) YAML export (`/config/export.yaml`) → contains all repositories and schedules; check whether credentials are included and protect the file accordingly.
- [ ] **C18** (P2) [test system] YAML import → the plan shows the changes before they are applied; afterwards the repositories are identical.
- [ ] **C19** (P2) YAML import with an error (e.g. unknown field, typo in the tool name) → error message in the plan, nothing applied.
- [ ] **C20** (P1) Create a schedule → sync starts at the scheduled time.
- [ ] **C21** (P2) Schedule with a following export → export starts only after a successful sync; no export after a failed sync.
- [ ] **C22** (P2) Edit and delete a schedule → takes effect at the next run time.
- [ ] **C23** (P3) Schedule becomes due while the same sync is still running → no second run.
- [ ] **C24** (P2) wget repository, mode *File list* with plain URLs from two different hosts and one `URL -> path` line → sync succeeds, files under the expected paths in `/data/<slug>/` (4.6).
- [ ] **C25** (P3) wget *File list*: empty list, 501 lines, two lines with the same target path, URL ending in `/`, URL with `?` → form error each time, nothing saved.

#### D. Jobs and logs

- [ ] **D1** (P1) Delete the newest job, start a new one → new, higher ID; an old ID is never reused (#39).
- [ ] **D2** (P1) Log starts with a `# run job <id>` header; a retry adds another header and keeps the old log.
- [ ] **D3** (P2) Delete a job → its log file is gone; a new job shows no foreign lines.
- [ ] **D4** (P2) Job detail during a sync → log updates live.
- [ ] **D5** (P2) Cleanup on the Jobs page → old finished jobs removed, running and queued jobs kept.
- [ ] **D6** (P3) Logs older than `MIRRORHUB_LOG_RETENTION_DAYS` are removed.
- [ ] **D7** (P3) Detail page of a deleted job → 404, no traceback.
- [ ] **D8** (P2) Failed job → the cause is in the last log lines and in the job list.
- [ ] **D9** (P3, from PR #50) Finished jobs older than the job and log retention (Settings) disappear within an hour; the newest finished job per repository and type stays; audit entry `jobs.retention` with actor `scheduler`.

#### E. Exports (sender)

- [ ] **E1** (P1) First export of a repository requested as `incr` → created as `full`.
- [ ] **E2** (P1) `incr` after changes to the mirror → contains only changed and new files; `deleted.txt` lists removed ones.
- [ ] **E3** (P1) Export directory contains `MANIFEST.sha256`, `files.json`, `manifest.json`, `deleted.txt` and, last, `.done`.
- [ ] **E4** (P1) `docker compose exec worker-export python -m mirrorhub.verify /export/<slug>/<export-id>` → ok.
- [ ] **E5** (P2) [test system] Copy an export, change one byte in the archive, `verify` → fails.
- [ ] **E6** (P2) Archive format `tar.zst` → export ok, importer applies it (with `zstd`).
- [ ] **E7** (P2) Set *Split parts at* small (e.g. 50 MB) → archive in parts; `verify` ok; importer applies it.
- [ ] **E8** (P2) Signed exports → signature present, importer with keyring and `require_signature = true` applies them.
- [ ] **E9** (P1) `manifest.json` → `tool_versions.mirrorhub_commit` equals the sender commit.
- [ ] **E10** (P2) Export with no changes since the last one → empty export is created, importer applies it without changing the tree.
- [ ] **E11** (P2) Drop folder during an export → the unfinished export has no `.done`; the pickup tool does not take it.
- [ ] **E12** (P2) Drop folder cleanup → leftovers without `.done` older than 24 h and empty directories removed; `.tmp` of a running export kept.
- [ ] **E13** (P2) Save and apply retention → old chains removed, the current chain stays complete.
- [ ] **E14** (P2) Clean up failed exports → leftovers removed, successful exports untouched.
- [ ] **E15** (P1) Markers: every published metadata copy has `.published` (commands in 3.1).
- [ ] **E16** (P1) Incremental since → see 3.3.
- [ ] **E17** (P2) After "Incremental since", the skipped export is marked *superseded* on the Exports page.
- [ ] **E18** (P2) Start "Incremental since" while another export of the same repository runs → error message, no second export.
- [ ] **E19** (P2) [test system] `MIRRORHUB_EXPORT_META_KEEP=3`, five exports → at most three copies plus protected bases remain; older ones can no longer be chosen for "Incremental since".
- [ ] **E20** (P2) `reconcile --dry-run` → shows what would be restored, changes nothing.
- [ ] **E21** (P1) [test system] Database loss: keep the state volume, delete the database, recreate the repositories via YAML, run `reconcile` → export history is back; the next `incr` builds on the last export, the importer applies it without `--rebase`.
- [ ] **E22** (P2) [test system] Delete one repository's `export-meta`, keep the database → `incr` refused with "run a full export".
- [ ] **E23** (P3) Second export of the same repository → faster than the first (hash cache `hashcache-<slug>.json`); note the duration.
- [ ] **E24** (P2) Disk space: temporarily set `disk_warn_percent` / `disk_critical_percent` in the settings below the current usage → one `low_disk` notification each (warning, critical), no repeat on every check; then set them back to 85/95.
- [ ] **E25** (P2, from PR #50) [test system] Save "Chains to keep" > 0 without pressing "Apply retention now" → within an hour the old chains are removed; audit entry `exports.retention` with actor `scheduler`. With 0 nothing is deleted.

#### F. Notifications

- [ ] **F1** (P1) Create a webhook channel, *Test* → receiver gets the message.
- [ ] **F2** (P1) SMTP channel, *Test* → mail arrives.
- [ ] **F3** (P1) File channel, *Test* → JSON file in `/export/_events/`.
- [ ] **F4** (P2) File channel with log mode `none`, `tail`, `full` → event file contains no log, the log tail, or the whole log.
- [ ] **F5** (P2) Trigger one rule each for `job_failed`, `job_succeeded`, `retry_exhausted`, `low_disk`, `export_published` → exactly one message each.
- [ ] **F6** (P2) `export_published` → contains slug, export ID and type (full/incr).
- [ ] **F7** (P2) Webhook target unreachable → retries, then audit entry `delivery_failed`; interface and jobs keep running normally.
- [ ] **F8** (P2) Existing SMTP/webhook channels after the update → test succeeds.
- [ ] **F9** (P2) The pickup tool transfers `_events/*.json`; the importer ignores `_events`.
- [ ] **F10** (P3) Delete a channel → its rules no longer fire, no errors in the log.

#### G. Receiver: importer

Installation

- [ ] **G1** (P1) `sha256sum -c mirrorhub-importer.pyz.sha256` → OK.
- [ ] **G2** (P1) `$MH --version` → commit equals the sender's footer.
- [ ] **G3** (P3) Run with Python < 3.12 (if available) → clear message instead of a traceback.

Applying exports

- [ ] **G4** (P1) First full into an empty tree → exit 0, `status` shows `last`.
- [ ] **G5** (P1) Full into a non-empty tree without `--rebase` → exit 5, tree unchanged.
- [ ] **G6** (P1) Incremental without an applied predecessor (chain gap) → exit 5, tree unchanged.
- [ ] **G7** (P1) Apply the same export again → nothing to do, exit 0.
- [ ] **G8** (P2) `import --dry-run` → plan printed; tree and state unchanged (`check` before/after identical).
- [ ] **G9** (P1) [test system] Tamper with a copy of an export, apply it → exit 3, tree unchanged.
- [ ] **G10** (P2) Export without `.done` in the inbox → not applied.
- [ ] **G11** (P2) Several waiting exports of one repository → `import --slug` applies them in chain order.
- [ ] **G12** (P2) `import --all`, one repository fails → the others are still applied, exit code ≠ 0.

Signature and tools

- [ ] **G13** (P2) `require_signature = true`, unsigned export → exit 4.
- [ ] **G14** (P2) Keyring with the wrong key → exit 4.
- [ ] **G15** (P3) `tar.zst` export without `zstd` in `PATH` → exit 8 with a hint.

Robustness

- [ ] **G16** (P2) [test system] Kill an import while unpacking → the next run resumes cleanly or reports exit 9 with a hint; afterwards a successful run, `check` exit 0.
- [ ] **G17** (P2) Two imports of the same repository at once → the second ends with exit 7.
- [ ] **G18** (P3) Staging inside a repository tree or on another file system → exit 2.
- [ ] **G19** (P2) File permissions in the tree → files readable by the web server (0644, directories 0755).
- [ ] **G20** (P3) File names with spaces and non-ASCII characters → applied correctly and escaped correctly in reports.

Superseded exports and versions

- [ ] **G21** (P1) Late export after "Incremental since" → see 3.3 (superseded, exit 0).
- [ ] **G22** (P2) "Incremental since" export and the late export are in the inbox together → the newer one is applied, the late one reported as superseded.
- [ ] **G23** (P2) Apply an older full explicitly with `--rebase` → applied, not skipped as superseded.
- [ ] **G24** (P2) Importer from a different commit than the sender → warning "built from different commits" in the log, import still runs.

Detection (tree check)

- [ ] **G25** (P1) Detection practical test → see 3.2.
- [ ] **G26** (P1) [test system] Change a file in the tree, then import → export is applied, exit 11; with `--drift-output DIR`, the report files are in `DIR/<slug>/`.
- [ ] **G27** (P2) `import --no-tree-check` → no check, exit 0.
- [ ] **G28** (P2) [test system] Change one byte, keep size and mtime (`touch -r`) → quick check exit 0; `check --hash` exit 11, `sha256.txt` names the file.
- [ ] **G29** (P2) [test system] Replace a file with a directory → `type.txt`; redirect a symlink → `symlink-target.txt`; truncate a file → `size.txt`; `touch` → `mtime.txt`.
- [ ] **G30** (P2) `summary.txt` → `total=` equals the sum of lines in all lists.
- [ ] **G31** (P2) `check --all` with one differing repository → all checked, exit 11, reports per repository.
- [ ] **G32** (P2) `check` after the inbox was emptied (exports removed) → works from `<state_dir>/inventories/`.
- [ ] **G33** (P3) `check` with a state file from an old importer and no inventory copy → clear message or fallback to the export in the inbox.

adopt

- [ ] **G34** (P2) `adopt --auto` → see 3.4.
- [ ] **G35** (P2) `adopt --export-id <id>` → adopts exactly this export.
- [ ] **G36** (P2) `adopt --auto --hash` → as G35, with a full content comparison.
- [ ] **G37** (P2) [test system] `adopt` on a differing tree → exit 10, no state file created.
- [ ] **G38** (P2) `adopt` with an existing state file → exit 5, file unchanged.
- [ ] **G39** (P3) `adopt` without a matching export in the inbox → clear error message.

Clients

- [ ] **G40** (P1) Client with `sources.list` pointing at the receiver mirror → `apt update` without errors, a package can be installed.
- [ ] **G41** (P2) After an incremental → `apt update` shows new package versions, no hash sum mismatch errors.

#### H. End to end through the real diode

- [ ] **H1** (P1) Sync → export → pickup → diode → inbox → import without manual intervention.
- [ ] **H2** (P1) Pickup transfers only exports with `.done`, never half-finished ones.
- [ ] **H3** (P2) Several exports transferred at once → importer applies them in chain order.
- [ ] **H4** (P2) Export delivered twice → skipped the second time, exit 0.
- [ ] **H5** (P2) Event files from `_events/` arrive on the receiver side.
- [ ] **H6** (P2) Largest repository: measure and note the duration of export, transfer, import and `check`.
- [ ] **H7** (P3) The pickup tool cleans up the drop folder after transfer the way MirrorHub expects (no conflicts with E12).

#### I. Backup and restore

- [ ] **I1** (P1) Backup command (2.1) → file exists; `sqlite3 <file> "pragma integrity_check"` → `ok`.
- [ ] **I2** (P1) The backup and `export-meta` are stored outside the Docker host.
- [ ] **I3** (P2) [test system] Restore database and `export-meta` → interface complete; the next `incr` is applied on the receiver without `--rebase`.
- [ ] **I4** (P3) [test system] Restore only the database, without `export-meta` → `incr` refused (as E22), full needed.
- [ ] **I5** (P2) Receiver: the state directory (`state_dir`) is backed up; after a restore, `status` is as before.
- [ ] **I6** (P3) [test system] VACUUM as in 4.4 → output "vacuum ok" or `ok`, file not larger afterwards; interface, jobs and exports unchanged.

#### J. Operations and monitoring

- [ ] **J1** (P1) `systemctl list-timers mirrorhub-check.timer` → timer active, next run set.
- [ ] **J2** (P2) Exit 11 in a timer run → unit `failed`, monitoring reports it.
- [ ] **J3** (P2) Imports are automated (timer or pickup hook), exit code ≠ 0 is monitored.
- [ ] **J4** (P2) Measure the `export-meta` size of the largest repository, divide by the number of copies → set `MIRRORHUB_EXPORT_META_KEEP`.
- [ ] **J5** (P2) Receiver: free space for staging is enough for the largest export.
- [ ] **J6** (P2) Sender: disk space of `/data`, `/export` and the state volume in monitoring (in addition to `low_disk`).
- [ ] **J7** (P3) Checkmk check (#40) on the receiver: evaluates exit code 11 and `summary.txt`.
- [ ] **J8** (P3) Look at the database size and its free share now and then (commands in 4.4).
- [ ] **J9** (P3, from PR #50) [test system] Audit purge as in 4.5: `--dry-run` shows the count; the real run writes the archive, leaves only younger entries plus `audit.purge`; `--older-than-days 30` is refused; afterwards a `DELETE FROM audit_log` is still rejected ("append-only").

### 3.6 Other open items

- [ ] Existing acceptance checks from the README (container health, proxy trust, signing keys, export chain after pickup, drop folder cleanup).
- [ ] #40 (Checkmk check): review and test on the receiver (other session).
- [ ] GitHub housekeeping: delete merged branches; enable *Settings → General → Automatically delete head branches*.
- [ ] Decide on audit log retention: how many days to keep (at least 90) and how long to keep the archives; check against retention obligations (audit, ISMS).

---

## 4. Key commands

### 4.1 Sender

| Purpose | Command |
| --- | --- |
| Container status | `docker compose ps` |
| Logs | `docker compose logs -f web` / `worker` / `worker-export` |
| Errors in the last 15 minutes | `docker compose logs --since 15m \| grep -i -E "error\|traceback\|exception"` |
| Smoke test | `MIRRORHUB_URL=https://<host>:<port> sh scripts/smoke_test.sh` |
| Database revision | see 1.1 step 5 |
| Commit of the running version | `docker compose exec web python -c "from mirrorhub import buildinfo; print(buildinfo.describe())"` |
| Database backup | `docker compose exec web python -m mirrorhub.backup <db> <target>` (see 2.1) |
| Database size / VACUUM | see 4.4 |
| Prune the audit log | see 4.5 |
| Verify an export | `docker compose exec worker-export python -m mirrorhub.verify /export/<slug>/<export-id>` |
| Restore export history from metadata | `docker compose exec web python -m mirrorhub.export.reconcile [--slug <slug>] [--dry-run]` |
| Build the importer | `make importer` |
| CA certificate | `make root-cert` |

### 4.2 Receiver

Shorthand: `MH="python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml"`

| Purpose | Command |
| --- | --- |
| Version / commit | `$MH --version` |
| Apply all waiting exports of a repo | `$MH import --slug <slug>` |
| All repos | `$MH import --all` |
| Plan only | `$MH import --slug <slug> --dry-run` |
| One export | `$MH import --export /srv/mirrorhub/inbox/<slug>/<export-id>` |
| Full over existing data | `$MH import --export <dir> --rebase` |
| Differences after import as files | `$MH import --slug <slug> --drift-output /var/lib/mirrorhub-reports` |
| Show state | `$MH status [--slug <slug>]` |
| Check the tree | `$MH check --slug <slug> \| --all [--hash] [--output-dir DIR]` |
| Lost state file | `$MH adopt --slug <slug> --auto [--hash]` or `--export-id <id>` |

Report files from `--output-dir` / `--drift-output` in `DIR/<slug>/`: `missing.txt`, `extra.txt`, `type.txt`, `size.txt`, `mtime.txt`, `symlink-target.txt`, `sha256.txt` (only with `--hash`), `summary.txt`. Evaluate for example with `wc -l DIR/<slug>/*.txt` or `grep '^total=' DIR/*/summary.txt`.

### 4.3 Importer exit codes

| Code | Meaning |
| --- | --- |
| 0 | ok, nothing to do, or skipped (superseded) |
| 2 | usage or configuration |
| 3 | export verification failed |
| 4 | signature |
| 5 | chain/base does not match, or state already exists (`adopt`) |
| 6 | unsafe archive |
| 7 | repository locked (another run active) |
| 8 | tool missing (`zstd`, `gpgv`) |
| 9 | I/O error or interrupted import |
| 10 | post-check failed / `adopt` finds differences |
| 11 | tree differs from the file list (after an import: the export was still applied) |

### 4.4 Database: cleanup, size and VACUUM (sender)

The database lives in the state volume at `/var/lib/mirrorhub/db/mirrorhub.sqlite3` (plus `-wal` and `-shm`).

**What is cleaned up when**

| Data | Cleanup |
| --- | --- |
| Expired sessions, login lockouts (24 h without a failed attempt) | automatically every 30 s (from PR #50; before only at start) |
| Delivered / finally failed notifications | automatically after 30 days |
| Finished jobs and their logs | Logs automatically after the job and log retention (Settings, default 30 days). The jobs themselves: from PR #50 automatically after the same period (hourly; the newest finished job per repository and type stays), before only by hand ("Clear finished" / "Delete" on the Jobs page) |
| Exports | "Apply retention now" / "Clean up failed" on the Exports page; from PR #50 the scheduler applies the "Chains to keep" rule hourly when it is above 0 |
| Deleting a repository | also removes its schedules, exports, snapshots and rules |
| Audit log | never automatically – append-only by database trigger; from PR #50 the operator can prune it by hand (4.5) |

**Why the file does not get smaller**

Deleted rows free space *inside* the file; SQLite reuses it for new data but does not return it to the file system. So the file only grows again once the free space is used up. SQLite writes the WAL file back regularly by itself. Only `VACUUM` makes the file smaller.

**Show size and free share** (works with the stack running)

```sh
docker compose exec web ls -l /var/lib/mirrorhub/db/
docker compose exec web python -c "import sqlite3; c=sqlite3.connect('file:/var/lib/mirrorhub/db/mirrorhub.sqlite3?mode=ro', uri=True); p=c.execute('pragma page_size').fetchone()[0]; n=c.execute('pragma page_count').fetchone()[0]; f=c.execute('pragma freelist_count').fetchone()[0]; print(f'{n*p/1e6:.1f} MB, of which free {f*p/1e6:.1f} MB')"
```

A `VACUUM` is only worth it when the free share is large – for example after deleting many jobs or after the first automatic job cleanup.

**Running VACUUM**

VACUUM rebuilds the file; it needs the write lock for its whole run and free space of about the size of the database. Therefore:

1. Back up as in 2.1 step 1.
2. Stop the services, run VACUUM, start again.

   From PR #50 (own command; puts temporary files next to the database instead of the small `/tmp`, checks integrity afterwards and shows the size before/after):

   ```sh
   docker compose stop worker worker-export web
   docker compose run --rm --no-deps web python -m mirrorhub.db vacuum
   docker compose up -d
   ```

   Before PR #50:

   ```sh
   docker compose stop worker worker-export web
   docker compose run --rm --no-deps -e SQLITE_TMPDIR=/var/lib/mirrorhub/db web python -c "import sqlite3; c=sqlite3.connect('/var/lib/mirrorhub/db/mirrorhub.sqlite3', isolation_level=None); c.execute('VACUUM'); c.execute('pragma wal_checkpoint(TRUNCATE)'); print(c.execute('pragma integrity_check').fetchone()[0])"
   docker compose up -d
   ```

   Expected: `vacuum ok: <before> MB -> <after> MB` or `ok`.
3. `docker compose ps` (all `healthy`) and the smoke test.

Notes:

- Without `SQLITE_TMPDIR`, the temporary files go to the container's `/tmp` (tmpfs, 256 MB); with larger databases VACUUM then fails with "database or disk is full".
- `docker compose run` runs the migrations first, as on a normal start; that is harmless.
- If VACUUM is interrupted, the old file stays valid and unchanged.

### 4.5 Pruning the audit log (sender, from PR #50)

The audit log is append-only: database triggers reject every UPDATE and DELETE, so neither the web interface nor a bug or a compromised application can remove entries. Pruning is therefore an operator task on the Docker host only; whoever has `docker` access there could change the database anyway, so this does not weaken the protection.

`python -m mirrorhub.audit purge --older-than-days N` (N at least 90) does in one transaction:

1. writes the entries to be removed to a JSON Lines archive (default `/var/lib/mirrorhub/backups/audit-archive-<UTC time>.jsonl`, mode 0600; `--archive PATH` for another file, which must not exist yet),
2. drops the delete trigger, deletes the entries, creates the trigger again,
3. records `audit.purge` (actor `cli`) with cut-off, count, id range and archive path.

If a step fails, everything is rolled back: nothing deleted, trigger in place. If the trigger is missing, the command refuses to run (then investigate, the log was not protected).

Procedure:

1. Back up as in 2.1 step 1.
2. Stop the services, check, prune, start:

   ```sh
   docker compose stop worker worker-export web
   docker compose run --rm --no-deps web python -m mirrorhub.audit purge --older-than-days 365 --dry-run
   docker compose run --rm --no-deps web python -m mirrorhub.audit purge --older-than-days 365
   docker compose up -d
   ```

   Expected: `N audit entries older than … would be deleted`, then `deleted N audit entries older than …; archive: …`.
3. Copy the archive off the host, like the database backup, and keep it as long as your retention rules require:

   ```sh
   docker compose cp web:/var/lib/mirrorhub/backups/<archive>.jsonl ~/mirrorhub-backups/
   ```

4. Optional: VACUUM (4.4) to shrink the file.

Read an archive: one JSON object per line (`id`, `ts`, `actor`, `ip`, `action`, `target_type`, `target_id`, `detail`), for example `grep '"action": "login.failure"' <archive>.jsonl | wc -l`.

### 4.6 wget repositories (sender, from PR #51)

The wget form has a switch *Download* with two modes.

**Whole directory tree** mirrors everything below *Root path* on *Host* recursively (`wget --recursive --no-parent`). A root of `/` mirrors the whole host. Two kinds of filters narrow the tree:

- **Architectures to keep** (from migration `0009`): e.g. `amd64, i386`. MirrorHub then skips every other known architecture and shows the generated regex under the field. An architecture counts as present when its name stands between `/`, `_`, `.`, `-` or at the end of the URL: `binary-arm64/`, `pool/stable/arm64/`, `foo_1.0_arm64.deb`, `foo-1.0.aarch64.rpm`, `tool-linux-arm64.tar.gz`. Aliases are covered (`amd64` = `x86_64`, `arm64` = `aarch64`, `i386` = `i486`–`i686`, `armhf` = `armv7*`, `armel` = `armv5`/`armv6`, `ppc64el` = `ppc64le`). Never skipped: files without an architecture in the path (`Release`, `InRelease`), `all`, `noarch`, and ambiguous short names (`arm`, `x86`, `x64`, `386`). Empty field = all architectures. Known limit: a package whose *name* is an architecture followed by a separator (e.g. `arm64-tools/`) is skipped too; keep it with an include regex or add it via a second repository.
- **Include/exclude regex**: POSIX regular expressions matched against the complete URL (`wget --accept-regex` / `--reject-regex`). Every URL is checked, including the directory pages wget follows: an include regex must therefore also match the parent directories, or wget never reaches the files. For "everything except …" use the exclude regex. The exclude regex and the architecture filter are combined into one `--reject-regex`.

Example, Docker's Ubuntu repository as a plain tree (amd64 only, stable channel only):

| Field | Value |
| --- | --- |
| Host / Root path | `download.docker.com` / `/linux/ubuntu` |
| Architectures to keep | `amd64` |
| Exclude regex | `/(test\|edge\|nightly)/` |

For APT repositories, debmirror is usually the better choice (signature check, only packages listed in the indexes, old packages removed, no `index.html` files from directory listings): use the preset **Docker CE (Ubuntu)** (debmirror, `download.docker.com`, `/linux/ubuntu`, suite `noble`, component `stable`, `amd64`, diff mode `none`). Before the first sync import `https://download.docker.com/linux/ubuntu/gpg` on the *Keys* page, adjust the suites, and start the first sync as a dry run.

**File list** downloads only the listed files, one per line, into `/data/<slug>/`:

```text
https://download.example.org/iso/image.iso
https://keys.other-site.net/release.asc -> keys/release.asc
```

- A plain URL is stored under its URL path: `https://download.example.org/iso/image.iso` → `iso/image.iso`. With `URL -> relative/path` you choose the target path yourself.
- Files may come from different hosts. *Host* and *Root path* are still required by the form, but in this mode they only describe the repository; enter the main host.
- At most **500 files** per repository (`MAX_EXTRA_FILES` in `src/mirrorhub/security/validators.py`; the form hint follows the constant).
- Rejected by the form: an empty list, two lines with the same target path, a URL ending in `/` without `URL -> path`, a target path that is absolute or contains `..`.
- URLs: `http` and `https` only, at most 500 characters. Not allowed: query strings and fragments (`?…`, `#…`, e.g. `getfile.php?id=4711` or signed cloud links), credentials in the URL (`user:pass@host`), IPv6 addresses. Tokens in a URL would end up in the job log and the database.

How a file-list sync runs, and what follows from it:

- One `wget` call per file, one after the other; each call opens a new connection.
- The first file that fails (404, timeout) stops the job; the remaining files are not tried. The retry policy then starts the whole job again. Check the job log for the last `$ wget …` line to find the file.
- Every sync downloads all files again (`wget -O` cannot compare timestamps). For large files or long lists, schedule the sync accordingly.
- Switching a repository from *File list* to *Whole directory tree* clears the stored list; files already in `/data/<slug>/` stay.

In the YAML configuration there is no mode field: an empty or missing `extra_files` means the whole tree, entries mean only those files (`extra_files: [{url: …, path: …}]`, path always explicit). Tree filters: `include_regex`, `exclude_regex`, `wget_keep_arches: [amd64, i386]` (before PR #51 the two regex fields were missing from the YAML export and were cleared when a wget repository was edited in the web interface).

---

## 5. What to do when …

| Incident | Action | Full export needed? |
| --- | --- | --- |
| Export lost in transit | Receiver: `status` shows `last`; sender: "Incremental since" with that export | no, as long as its metadata is kept |
| Pickup delivers an export twice or late | nothing; the importer skips it as applied or superseded | no |
| Sender database lost, state volume intact | restore repositories (backup or YAML import); the export history is restored from `export-meta` | no |
| Receiver state file lost, tree intact | `adopt --slug <slug> --auto` | no |
| Files deleted or damaged on the receiver | `check --output-dir` shows what; repair currently with full + `--rebase` (repair export planned in #43) | currently yes |
| Incremental fails with "run a full export" | "Incremental since" with the receiver's last state; if that is not possible: full + `--rebase` | only if no matching copy is left |
| Importer log "importer and sender were built from different commits" | rebuild the importer from the running sender checkout (`make importer`) | no |
| Sender state volume lost, backup available | restore the backup (database and `export-meta`), then as "export lost in transit" | no |
| State volume lost, no backup | full export, `--rebase` on the receiver | yes |
| Database file is large although a lot was deleted | check size and free share, then VACUUM (4.4) | no |
| wget file-list sync fails | job log: the last `$ wget …` line names the file; fix or remove the URL, sync again (4.6) | no |

---

## 6. Further reading

- [`acceptance-p1.md`](acceptance-p1.md) – P1 acceptance checklist (the 45 P1 tests from 3.5 with sign-off fields)
- [`importer.md`](importer.md) – importer in detail (installation, superseded exports, `adopt`, detection, report files)
- [`export-contract.md`](export-contract.md) – export format and guarantees
- README → *Testing → Acceptance checks*; README → *Backup → Database size and VACUUM* (from PR #50)
- Design document "Robust export base and recovery without a full export": https://claude.ai/artifact/7fMEWyCPdgNzXJRerFAERL
- Open: #43 remainder (repair export, `importer inventory`, `adopt --accept-drift`), #40 (Checkmk), PR #50 (web GUI review)
