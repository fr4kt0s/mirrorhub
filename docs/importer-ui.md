# Importer web UI

A small web interface for the receiver importer. It offers everything the command
line does (`import`, `check`, `adopt`, `status` with all options) and shows the
output live. It is part of `mirrorhub-importer.pyz` (standard library only) and
needs no installation beyond Python 3.12.

## Start

From the importer file built with `make importer`:

```sh
python3 mirrorhub-importer.pyz --config importer.toml serve
# MirrorHub importer UI on http://127.0.0.1:8080/ (config …, no login)
```

`--listen HOST:PORT` (or `MIRRORHUB_IMPORTER_UI_LISTEN`) changes the address; the
default `127.0.0.1:8080` is reachable from the same host only (for example through
`ssh -L 8080:127.0.0.1:8080 receiver`).

### Docker

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
- the port is published on `127.0.0.1` only.

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
  check, each list downloadable as text.
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
applied exports); repairing a tree needs a new full export from the sender.

### Where the data comes from

| Shown | Source |
|---|---|
| runs of the UI and their output | `<state_dir>/ui-runs/<n>.json` and `<n>.log` (last 200 kept, last 50 shown); kept across restarts |
| history, last import, failures, tree status | event files in `events_dir` |
| difference lists | `reports_dir` if set in `importer.toml` (then timer runs write them too), else `<state_dir>/reports`, written by checks and imports started from the UI |
| pending exports, export details | the inbox (`manifest.json`, archive sizes); verified only when imported |

## Access

There are no user accounts.

| Setting | Effect |
|---|---|
| default | no login; listens on 127.0.0.1 |
| `MIRRORHUB_IMPORTER_UI_PASSWORD` | password page (no user name); session ends when the server restarts |
| `MIRRORHUB_IMPORTER_UI_HOSTS` | comma-separated host names accepted in the browser address (besides `localhost` and IP addresses) |

Because a click starts an import, the UI also protects itself against other web
pages open in the same browser: every form carries a token, posts from another
origin are refused, and requests for unknown host names are refused (DNS
rebinding). Listening on another address than 127.0.0.1 without a password prints
a warning; then restrict access by firewall or port binding.

## Limits

- Up to 4 MB of output per run are shown (the oldest lines are dropped).
- A run that was active when the UI stopped is shown with exit code −1 (result
  unknown); the importer's own state, journal and event files tell what happened.
- No scheduling: keep the systemd timer for regular imports and checks
  (`docs/importer.md`).
