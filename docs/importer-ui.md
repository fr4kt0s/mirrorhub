# Importer web UI

A small web interface for the receiver importer. It offers everything the command
line does (`import`, `check`, `adopt`, `status` with all options) and shows the
output live. It is part of `mirrorhub-importer.pyz` (standard library only) and
needs no installation beyond Python 3.12.

## Setup step by step

The examples use the receiver paths of this installation (inbox
`/mnt/repository/export`, mirror trees below `/mnt/import/mirrorhub/data`); adjust
them to yours.

### 1. Build the importer file

On the sender host, in the deployed checkout:

```sh
git pull && make importer          # dist/mirrorhub-importer.pyz + .sha256
```

Carry both files to the receiver, then check them there:

```sh
sha256sum -c mirrorhub-importer.pyz.sha256
python3 mirrorhub-importer.pyz --version    # 0.1.0 (<commit>), same as the sender footer
install -D -m 0644 mirrorhub-importer.pyz /opt/mirrorhub/mirrorhub-importer.pyz
```

The web UI is part of this file; nothing else is installed (Python ≥ 3.12, plus
`zstd` for `tar.zst` exports and `gpgv` for signatures).

### 2. Configure `importer.toml`

```toml
export_dir   = "/mnt/repository/export"        # inbox (read only for the importer)
mirror_root  = "/mnt/import/mirrorhub/data"    # one tree per repository: <mirror_root>/<slug>
state_dir    = "/mnt/import/mirrorhub/state"   # receiver state, locks, UI runs; its own directory
events_dir   = "/var/lib/mirrorhub-importer/events"    # history in the UI, Checkmk
reports_dir  = "/var/lib/mirrorhub-importer/reports"   # difference lists in the UI
# events_keep_days = 30
# keyring = "/etc/mirrorhub/trustedkeys.gpg"
# require_signature = false
```

`events_dir` and `reports_dir` are optional, but without them the UI shows no
history, no tree status and only the difference lists of checks started from the
UI itself. All keys: [reference](#configuration-keys-importertoml) below.

Moving an existing state directory (for example from `/mnt/import` to
`/mnt/import/mirrorhub/state`): while no import runs, move `*.json`, `*.lock` and
`inventories/` to the new directory and change `state_dir`.

### 3. Directories and permissions

The user that runs the UI runs every command from it, so it needs the same rights
as the import timer: read the inbox, write `mirror_root`, `state_dir`,
`events_dir` and `reports_dir`. Run both as the same user (on NFS with the same
uid), for example `mirrorhub`:

```sh
install -d -o mirrorhub -g mirrorhub /var/lib/mirrorhub-importer
sudo -u mirrorhub python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml status
```

A tree copied from another system keeps its foreign owner; give it to this user
first (`sudo chown -R mirrorhub: <mirror_root>/<slug>`), otherwise imports stop at
the permission check (exit code 9) and *Check* shows a warning. Details:
[importer.md](importer.md), "User and permissions".

### 4. Start by hand and test

```sh
sudo -u mirrorhub python3 /opt/mirrorhub/mirrorhub-importer.pyz \
  --config /etc/mirrorhub/importer.toml serve
# MirrorHub importer UI on http://127.0.0.1:8080/ (config …, no login)
```

From your workstation: `ssh -L 8080:127.0.0.1:8080 receiver`, then open
`http://localhost:8080/`. Check: the repositories appear, *Show status* prints the
state, *Dry run* on a repository plans without changing anything.

`--listen HOST:PORT` (or `MIRRORHUB_IMPORTER_UI_LISTEN`) changes the address.

### 5. Run it permanently (systemd)

`/etc/systemd/system/mirrorhub-importer-ui.service`:

```ini
[Unit]
Description=MirrorHub importer web UI
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
User=mirrorhub
Group=mirrorhub
Environment=MIRRORHUB_IMPORTER_CONFIG=/etc/mirrorhub/importer.toml
# optional: password, other address, host names (file mode 0600)
EnvironmentFile=-/etc/mirrorhub/importer-ui.env
ExecStart=/usr/bin/python3 /opt/mirrorhub/mirrorhub-importer.pyz serve
Restart=on-failure
UMask=0022

[Install]
WantedBy=multi-user.target
```

```sh
systemctl daemon-reload && systemctl enable --now mirrorhub-importer-ui
journalctl -u mirrorhub-importer-ui -f
```

Stopping the service while a run is active cancels that run; an interrupted
import continues from its journal on the next import.

Keep the import timer for regular imports (the UI has no scheduler); give it the
same `importer.toml`, so its runs show up in the UI's history and reports.

### 6. Access from other hosts (optional)

`/etc/mirrorhub/importer-ui.env` (`chmod 600`):

```sh
MIRRORHUB_IMPORTER_UI_LISTEN=0.0.0.0:8080
MIRRORHUB_IMPORTER_UI_PASSWORD=a-long-random-password
MIRRORHUB_IMPORTER_UI_HOSTS=importer.example.org
```

The UI speaks plain HTTP: put a TLS reverse proxy in front of it (or keep using
the SSH tunnel), otherwise the password crosses the network unencrypted. Limit the
port with the firewall.

### Docker instead of steps 1, 4 and 5

```sh
make importer-image            # image mirrorhub-importer from Dockerfile.importer
docker compose -f contrib/importer-ui/compose.yml up -d
```

The image contains exactly the `.pyz` from `make importer` plus `zstd` and `gpgv`,
runs as user 10001 and starts `serve` on port 8080. Adjust
`contrib/importer-ui/compose.yml` first:

- mount the host directories **at the same paths** inside the container, so
  `importer.toml` works unchanged for the CLI on the host and in the container
  (inbox read-only; mirror tree, state, events and report directories writable);
- `user:` must be the owner of the mirror tree and the state directory on the host
  (`id -u`, `id -g`), also on NFS;
- the port is published on `127.0.0.1` only; password and host names as in step 6
  under `environment:`.

Any CLI command also runs in the container, for example
`docker compose -f contrib/importer-ui/compose.yml run --rm importer-ui import --all`.

## Pages

- **Repositories**: every repository in the inbox or with a receiver state, with
  - *Last import*: the applied export (link to its page) and when; a failed import
    in red with its error until the next success;
  - *Tree*: result of the newest tree check (after an import, `check`, `adopt`):
    *matches* or the number of differences per kind, quick or SHA-256 and how long
    ago, when the last SHA-256 check ran, link to the report;
  - *Pending*: exports in the inbox not applied or superseded yet (links);
  - buttons *Import*, *Dry run*, *Check*, *Check SHA-256*; link to the history.
- **History**: imports, failures, superseded exports, tree checks and every import
  run (also timer and shell runs, *trigger* `cli`, and runs from the UI, `ui`),
  newest first, per repository or all. Needs `events_dir` in `importer.toml`.
- **Report** (per repository): the newest difference lists — missing, extra, type,
  size, mtime, symlink target, sha256 — with the export and the time of the
  check, each list downloadable as text. With differences: the **repair request**
  (`repair-<slug>.json` and its `.sha256`) for download, to carry to the sender
  (see [importer.md](importer.md), "Repairing the tree").
- **Export** (per export in the inbox): status (pending, applied, superseded),
  mode, base export and whether it fits the receiver state, creation time, file,
  payload and deletion counts, archive size and parts, signature present, sender
  commit. Buttons: *Import this export* and *Dry run* when it fits; for a full
  export over an existing state *Plan rebase (dry run)*.
- **Import**, **Check tree**, **Adopt**, **Status** forms with the CLI options.
- **Runs**: command line, live output (readable log lines, progress every 10 s),
  duration, exit code and meaning; *Cancel* (SIGTERM; an interrupted import
  continues from its journal on the next import).

### Rebase only after a plan

A full export over an existing tree (`--rebase`) deletes every path that is not
in its `files.json`. The UI offers it only on the export's page: *Plan rebase*
runs the import as a dry run with `--rebase`; its page lists every change
(`A`/`C`/`D` lines) and the counts, and offers *Apply rebase (N deletions)*. That
button works for an hour and only for the same export. `--discard-staging` stays
in the import form behind a confirmation.

A full export that is already applied cannot be applied again (the importer skips
applied exports). To repair a tree, use the repair request on the report page
instead of a full export.

### Where the data comes from

| Shown | Source |
|---|---|
| runs of the UI and their output | `<state_dir>/ui-runs/<n>.json` and `<n>.log` (last 200 kept, last 50 shown); kept across restarts |
| history, last import, failures, tree status | event files in `events_dir` |
| difference lists | `reports_dir` if set in `importer.toml` (then timer runs write them too), else `<state_dir>/reports`, written by checks and imports started from the UI |
| pending exports, export details | the inbox (`manifest.json`, archive sizes); verified only when imported |

## Access

There are no user accounts.

Default: no login, listening on 127.0.0.1 only. Password and host names: step 6
and the [environment variables](#environment-variables).

Because a click starts an import, the UI also protects itself against other web
pages open in the same browser: every form carries a token, posts from another
origin are refused, and requests for unknown host names are refused (DNS
rebinding). Listening on another address than 127.0.0.1 without a password prints
a warning; then restrict access by firewall or port binding.

## Reference

### Environment variables

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `MIRRORHUB_IMPORTER_CONFIG` | every command, `serve` | — (Docker image: `/config/importer.toml`) | path of `importer.toml` when `--config` is not given |
| `MIRRORHUB_IMPORTER_UI_LISTEN` | `serve` | `127.0.0.1:8080` (Docker image: `0.0.0.0:8080`) | address and port, `HOST:PORT`; `--listen` wins |
| `MIRRORHUB_IMPORTER_UI_PASSWORD` | `serve` | unset = no login | password for the UI (no user name); the session ends when the UI restarts |
| `MIRRORHUB_IMPORTER_UI_HOSTS` | `serve` | unset | extra host names allowed in the browser address, comma-separated (`localhost` and IP addresses are always allowed) |
| `MIRRORHUB_IMPORTER_TRIGGER` | event files | `cli` | `trigger` field of events; the UI sets `ui` for its runs. Not set by hand. |
| `NO_COLOR`, `TERM=dumb` | text log lines | — | no colours in `--log-format text` |
| `MIRRORHUB_COMMIT` | `make importer-image`, `scripts/build_importer.py` | from git | commit shown by `--version` and the UI when the build has no git checkout (the Docker build) |

### Configuration keys (`importer.toml`)

Relative paths are relative to the directory of `importer.toml`.

| Key | Required | Default | Meaning |
|---|---|---|---|
| `export_dir` | yes | — | inbox: `<export_dir>/<slug>/<export-id>/`; only read |
| `mirror_root` | yes | — | mirror trees: `<mirror_root>/<slug>/` |
| `state_dir` | yes | — | receiver state (`<slug>.json`), locks, `inventories/`, UI runs (`ui-runs/`), default reports (`reports/`); not inside a mirror tree, best its own directory |
| `staging_dir` | no | `<mirror_root>/.mirrorhub-import` | temporary extraction; must be on the same file system as `mirror_root` |
| `keyring` | no | — | keyring for `gpgv` (signed exports) |
| `require_signature` | no | `false` | refuse unsigned exports |
| `events_dir` | no | off | event files: UI history and tree status, Checkmk ([checkmk.md](checkmk.md)) |
| `events_keep_days` | no | `30` | delete event files older than this many days (`0` keeps all) |
| `reports_dir` | no | off (UI runs: `<state_dir>/reports`) | difference lists of every `check` and import tree check, also from timer runs; shown on the report pages |

### `serve` options

| Option | Meaning |
|---|---|
| `--listen HOST:PORT` | address (default from `MIRRORHUB_IMPORTER_UI_LISTEN`, else `127.0.0.1:8080`) |
| `--config FILE` | `importer.toml` (or `MIRRORHUB_IMPORTER_CONFIG`) |
| `--log-level`, `--log-format` | as for every command; the UI's own request log goes to stderr |

## Limits

- Up to 4 MB of output per run are shown (the oldest lines are dropped).
- A run that was active when the UI stopped is shown with exit code −1 (result
  unknown); the importer's own state, journal and event files tell what happened.
- No scheduling: keep the systemd timer for regular imports and checks
  (`docs/importer.md`).
