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

- **Repositories**: every repository in the inbox or with a receiver state, the
  last applied export and when, how many exports are pending (not yet applied or
  superseded), and buttons for *Import*, *Dry run*, *Check* and *Check SHA-256*.
- **Import**: all repositories, one repository or one export directory (must be
  below `export_dir`); options dry run, full check, verify first, no tree check,
  drift output directory; `--rebase` and `--discard-staging` only with the
  confirmation box ticked.
- **Check tree**, **Adopt**, **Status**: the same options as the CLI.
- **Runs**: the last 50 runs since the server started, with the full command
  line, live output (readable log lines, progress every 10 s), duration, exit code
  and its meaning. *Cancel* stops a running command (SIGTERM); an interrupted
  import continues from its journal on the next import.

One run at a time. Each run is the real CLI in its own process, so locks, exit
codes, event files (`events_dir`) and the receiver state are the same as on the
command line, and a run started from the UI and a timer run never apply at the
same time (the second one stops with exit code 7).

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

- Runs and their output are kept in memory only (last 50, up to 4 MB of output
  each) and are gone after a restart; the importer's own log lines, event files and
  state are unaffected.
- No scheduling: keep the systemd timer for regular imports and checks
  (`docs/importer.md`).
