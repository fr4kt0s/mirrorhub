# Monitoring MirrorHub with Checkmk

MirrorHub writes its state as small JSON event files. A Checkmk local check on the
receiving host, `contrib/checkmk/mirrorhub_events.py`, reads them and creates one
service per repository and topic. Nothing has to cross the data diode in the
reverse direction, and the check needs only Python's standard library.

```
 Sender (MirrorHub)                         Receiver
 ──────────────────                         ────────────────────────────────────────
 file channel ──► /export/_events/*.json ─► pickup tool ─► <inbox>/_events/*.json ─┐
                                            (data diode)                            │
                                            importer ─► events_dir/*.json ──────────┤
                                                                                    ▼
                                            Checkmk agent: local check mirrorhub_events
```

| Source | What it reports | Configured in |
|---|---|---|
| Sender file channel | syncs, exports, low disk | MirrorHub web UI, *Notifications* |
| Receiver importer | imports, failures, tree checks, every import run | `importer.toml` (`events_dir`) |

## Services

| Service | OK | WARN | CRIT |
|---|---|---|---|
| MirrorHub sync &lt;repo&gt; | last sync succeeded, younger than `sync_warn_hours` | last success older than `sync_warn_hours` | last sync failed, or last success older than `sync_crit_hours` |
| MirrorHub export &lt;repo&gt; | last export published, younger than `export_warn_hours` | older than `export_warn_hours` | last export job failed, or older than `export_crit_hours` |
| MirrorHub sender disk | no `low_disk` event within `disk_alert_hours` | warning level | critical level |
| MirrorHub event feed | sender event files readable | unreadable files, or no event for `feed_warn_hours` (if > 0) | (UNKNOWN: directory missing) |
| MirrorHub import &lt;repo&gt; | last import succeeded, nothing published that is not imported | an export published on the sender is not imported after `import_lag_warn_hours` | last import failed (until the next success), or not imported after `import_lag_crit_hours` |
| MirrorHub tree &lt;repo&gt; | tree matches `files.json` of the last applied export | differences (`tree_drift_state`, default WARN); last `check` failed; no tree check for `tree_check_warn_hours` or no SHA-256 check for `tree_hash_warn_hours` (if > 0) | differences with `tree_drift_state` 2 |
| MirrorHub importer | importer event files readable, last import run exit code 0 or 11 | unreadable files; last run ended with another exit code; no import run for `importer_run_warn_hours` (if > 0) | (UNKNOWN: directory missing) |

The import, tree and importer services appear only when `importer_events_dir` is
set. Metrics: age of the last sync, export, import and import run, import lag,
duration and file count of the last import, number of tree differences, archive
size and file count of the last export, sender disk usage.

"Not imported yet" compares the export id of the newest `export_published` event
from the sender with the newest applied export on the receiver. An export skipped
as superseded is not counted as missing once a later export has been applied.

## 1. Sender: file channel

On the MirrorHub web UI, *Notifications*:

1. Create a channel of type **File**. Log detail `none` is enough for Checkmk
   (`tail` adds the last lines of the job log to failure events).
2. *Test* the channel: a JSON file appears in `/export/_events/` (host:
   `HOST_EXPORT_DIR/_events/`).
3. Create event rules for this channel, for all repositories: `job_succeeded`,
   `job_failed`, `export_published`, `low_disk`.

Details: README, section "File channel (events through the data diode)".

## 2. Transfer: pickup tool

Configure the pickup tool to transfer `_events/*.json` (and `*.log` if you use log
detail `full`) along with the exports, into the importer's inbox, for example
`/mnt/repository/export/_events/`. Ignore hidden `.tmp` files. The importer ignores
directories starting with `_`.

The sender deletes files the pickup tool did not take after the channel retention.
On the receiver, delete old files yourself; the Checkmk check keeps its own state
and does not need them, for example with a daily timer:

```sh
find /mnt/repository/export/_events -name '*.json' -mtime +30 -delete
```

## 3. Receiver: importer event files

Use an importer built from a MirrorHub version with event files
(`python3 mirrorhub-importer.pyz --version`; built with `make importer` on the
sender from the same checkout). Add to `importer.toml`:

```toml
events_dir = "/var/lib/mirrorhub-importer/events"
events_keep_days = 30
```

Pick a directory the Checkmk agent can read, preferably on a local disk of the host
that runs both the importer and the agent. The importer creates it on the first
run and deletes its own files older than `events_keep_days`.

Run an import and look at the files; every `import` run writes at least an
`import_run` event, also when nothing is pending:

```sh
python3 mirrorhub-importer.pyz --config importer.toml import --all
ls /var/lib/mirrorhub-importer/events/
```

The events are described in [importer.md](importer.md), section "Event files for
monitoring".

For the tree service to show content checks, run a hashed check regularly, for
example weekly with a systemd timer (`docs/importer.md`, section "Detecting changes
in the tree"):

```sh
python3 mirrorhub-importer.pyz --config importer.toml check --all --hash \
  --output-dir /var/lib/mirrorhub-importer/check
```

Run it on the same host as the import, at a time when no import runs: both use the
same repository lock, and the second one stops with exit code 7.

## 4. Receiver: install the local check

The agent runs local checks from `/usr/lib/check_mk_agent/local/` (Linux). The
script needs Python 3.9 or newer.

```sh
install -m 0755 contrib/checkmk/mirrorhub_events.py \
  /usr/lib/check_mk_agent/local/mirrorhub_events
```

To run it only every 5 minutes instead of on every agent call, install it into a
subdirectory named after the interval in seconds; the agent then runs it in the
background and serves the cached output:

```sh
install -d /usr/lib/check_mk_agent/local/300
install -m 0755 contrib/checkmk/mirrorhub_events.py \
  /usr/lib/check_mk_agent/local/300/mirrorhub_events
```

Create `/etc/check_mk/mirrorhub_events.json` (another path: environment variable
`MIRRORHUB_EVENTS_CONFIG`). All keys are optional; this example uses the paths from
above:

```json
{
  "events_dir": "/mnt/repository/export/_events",
  "importer_events_dir": "/var/lib/mirrorhub-importer/events",
  "state_file": "/var/lib/check_mk_agent/mirrorhub_events_state.json",
  "sync_warn_hours": 26,
  "sync_crit_hours": 50,
  "export_warn_hours": 26,
  "export_crit_hours": 50,
  "import_lag_warn_hours": 6,
  "import_lag_crit_hours": 24,
  "importer_run_warn_hours": 3,
  "tree_hash_warn_hours": 192,
  "repos": {
    "archlinux": {"export": false, "import": false}
  }
}
```

## 5. Test by hand

```sh
/usr/lib/check_mk_agent/local/mirrorhub_events
```

Each line is one service: state (0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN), name, metrics,
text. Example:

```
0 "MirrorHub event feed" - 6 event files in /mnt/repository/export/_events, newest event 1.0 h ago
0 "MirrorHub importer" last_run_age=1800;10800 12 event files in /var/lib/mirrorhub-importer/events, last import run (all) 30 min ago, exit code 0
0 "MirrorHub sync download.docker.com" last_success_age=10800;93600;180000 Last sync OK 3.0 h ago (job 41)
0 "MirrorHub export download.docker.com" last_export_age=9000;93600;180000|archive_bytes=812000000|files=37 Last export 20261123T224714Z-incr (incr, 37 files, 0.81 GB) 2.5 h ago
0 "MirrorHub import download.docker.com" last_import_age=1800|import_duration=42.0|import_files=37|import_lag=0;21600;86400 Last import 20261123T224714Z-incr (incr, 37 files, 42.0 s) 30 min ago
1 "MirrorHub tree download.docker.com" tree_differences=1 Tree differs from 20261123T224714Z-incr in 1 places (missing 1), quick check (check) 2.0 h ago; run 'check --output-dir' for the lists
```

The agent output contains the same lines in the `<<<local>>>` section:

```sh
check_mk_agent | sed -n '/<<<local/,/<<</p'
```

## 6. Checkmk: discover the services

1. *Setup → Hosts*, open the receiving host, *Save & run service discovery*
   (or *Services*).
2. Accept the new `MirrorHub …` services (*Accept all*).
3. *Activate pending changes*.

New repositories create new services: run the discovery again, or let the periodic
service discovery of the site add them. Removed repositories leave services that
stay at their last state; remove them in the discovery (or list them in
`ignore_repos` first).

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `events_dir` | `/srv/diode-inbox/_events` | sender event files after the diode |
| `importer_events_dir` | `""` (off) | importer `events_dir`; enables the import, tree and importer services |
| `state_file` | `/var/lib/check_mk_agent/mirrorhub_events_state.json` | newest result per repository, so services keep their state when files are deleted |
| `sync_warn_hours`, `sync_crit_hours` | 26, 50 | age of the last successful sync |
| `export_warn_hours`, `export_crit_hours` | 26, 50 | age of the last published export |
| `disk_alert_hours` | 24 | how long a `low_disk` event keeps the disk service WARN/CRIT |
| `feed_warn_hours` | 0 (off) | WARN when no sender event arrived for this long |
| `import_lag_warn_hours`, `import_lag_crit_hours` | 6, 24 (0 = off) | a published export not imported for this long |
| `importer_run_warn_hours` | 0 (off) | WARN when the importer did not run for this long; about 2–3 × the import timer interval |
| `tree_drift_state` | 1 | state for tree differences: 1 WARN, 2 CRIT |
| `tree_check_warn_hours` | 0 (off) | WARN when no tree check (after an import or `check`) for this long |
| `tree_hash_warn_hours` | 0 (off) | WARN when no SHA-256 check (`check --hash`, `--full-check`) for this long; 192 for a weekly check |
| `ignore_repos` | `[]` | repositories without services |
| `repos` | `{}` | per repository: any hour limit above, `tree_drift_state`, and `"export"`, `"import"`, `"tree"`: `false` to hide that service |

## Troubleshooting

| Symptom | Cause / what to do |
|---|---|
| No `MirrorHub` services | Run the script by hand (step 5); then the service discovery (step 6). |
| `MirrorHub event feed` UNKNOWN "events directory missing" | `events_dir` is wrong, or the pickup tool has not created `_events` yet. |
| `MirrorHub importer` UNKNOWN | `importer_events_dir` is wrong, or no import has run since `events_dir` was set. |
| No import/tree services | `importer_events_dir` not set, or no importer event for that repository yet. |
| Import "published … not imported yet" | The export reached the sender's drop folder but is not applied: the pickup tool has not transferred it, the import timer is not running, or the import failed (then the service is CRIT with the error). `importer status --slug <repo>`. |
| Tree "differs … in N places" | Files changed on the receiver outside the importer. `importer check --slug <repo> --output-dir DIR` lists them in `DIR/<repo>/*.txt`; a repair export is planned (#43), until then a full export with `import --rebase`. |
| Importer WARN "exit code 7" | Two importer runs at the same time (lock); separate the timers. |
| Services keep an old state after a fix | The state comes from the newest event; it changes with the next import, check or sync. To start over, delete `state_file`. |

The script always prints valid lines; an internal error appears as
`3 "MirrorHub event feed" - check failed: …`.
