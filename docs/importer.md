# MirrorHub importer (receiver side)

Status: experimental. Tested against hand-built fixtures that follow
`export-contract.md` and in a round trip with the real export writer
(`tests/test_export_base.py`), not yet against a real data diode.

## Installation on the receiver

The importer needs only the Python standard library (Python 3.12 or newer), plus
`zstd` for `tar.zst` exports and `gpgv` when a keyring is configured. Database, web
application and the packages from `requirements.txt` are not needed.

Build it as one file on the sender side (or download the `mirrorhub-importer`
artifact of the `check` workflow run for the commit you deploy):

    make importer                      # or: python3 scripts/build_importer.py
    # -> dist/mirrorhub-importer.pyz and dist/mirrorhub-importer.pyz.sha256

Carry both files to the receiver, check and run:

    sha256sum -c mirrorhub-importer.pyz.sha256
    python3 mirrorhub-importer.pyz --version
    python3 mirrorhub-importer.pyz --config importer.toml import --slug ubuntu

The build is reproducible: the same commit and Python version give the same bytes,
so the checksum can be compared with a build made elsewhere. The archive contains
only `mirrorhub/__init__.py`, `mirrorhub/verify.py`, `mirrorhub/buildinfo.py`,
`mirrorhub/importer/` and `mirrorhub/_build.py` with the commit it was built from.

Build the importer from the same checkout that runs the sender (`git pull && make
importer` on the sender host). `--version` shows the commit, for example
`mirrorhub.importer 0.1.0 (9d2440860609)`; `-dirty` means the checkout had local
changes. Each export records the sender's commit in `manifest.json`
(`tool_versions.mirrorhub_commit`); when it differs from the importer's commit, the
importer logs the warning "importer and sender were built from different commits"
and imports anyway. The sender shows its commit in the footer of every page.

Without the archive, copying those three items into a directory and running
`python3 -m mirrorhub.importer …` from there works the same way.

## Usage

All examples use `python -m mirrorhub.importer`; with the archive, write
`python3 mirrorhub-importer.pyz` instead.

    python -m mirrorhub.importer --config importer.toml import --slug ubuntu
    python -m mirrorhub.importer --config importer.toml import --slug ubuntu --dry-run
    python -m mirrorhub.importer --config importer.toml import --export /inbox/ubuntu/<id>
    python -m mirrorhub.importer --config importer.toml status
    python -m mirrorhub.importer --config importer.toml check --slug ubuntu [--hash] [--output-dir DIR]
    python -m mirrorhub.importer --config importer.toml check --all
    python -m mirrorhub.importer --config importer.toml adopt --slug ubuntu --auto [--hash]
    python -m mirrorhub.importer --config importer.toml adopt --slug ubuntu --export-id <id>

`--slug` applies all published exports (directories with `.done`) from
`export_dir/<slug>` in timestamp order. `--dry-run` plans only the first pending export.
The config path can also be set with `MIRRORHUB_IMPORTER_CONFIG`. See
`importer.example.toml`.

### Output and log level

The importer writes two kinds of output:

- **stdout, plain text** for people: the plan (`--dry-run`), `applied …`,
  `nothing to import …`, `status`, `check` results and the first differences. It does
  not depend on the log level.
- **stderr, log lines**: time, level, event, then `slug` and `export_id`, then further
  fields. Error messages (`error: …`) are also printed as plain text on stderr.

`--log-format` chooses how the log lines look: `auto` (default) writes readable text
when stderr is a terminal and one JSON object per line otherwise (systemd journal,
file, pipe), so collected logs stay machine-readable; `text` or `json` force one.
Text lines show warnings in yellow and errors in red on a terminal (not with
`NO_COLOR` set or `TERM=dumb`):

    20:32:46 DEBUG   chain check passed      ubuntu  20261101T000000Z-full  mode=full  rebase=False
    20:32:47 INFO    progress                ubuntu  20261101T000000Z-full  staging   49.1 %  14726/30001 files  28.8 MiB/58.6 MiB  28.8 MiB/s  elapsed 1s  left 1s
    20:32:50 INFO    applied                 ubuntu  20261101T000000Z-full

    {"ts": "2026-11-24T19:32:50+00:00", "level": "info", "event": "applied", "slug": "ubuntu", "export_id": "20261101T000000Z-full"}

Text lines use local time, JSON lines UTC. `--log-level` (`debug`, `info` (default),
`warning`, `error`) controls which lines appear in either format. `--log-level`,
`--log-format` and `--config` may be given before or after the command; a value
after the command wins:

    python -m mirrorhub.importer --config importer.toml import --slug ubuntu --log-level debug

| Level | Log lines on stderr |
|---|---|
| `error` | failures only |
| `warning` | plus unsigned exports, superseded exports, commit mismatch, tree differences, resumed imports |
| `info` | plus `staged`, `applied`, `already applied`, `adopted`, `progress` and `phase done` of long phases |
| `debug` | plus every step: config, pending exports, verification, signature, lock, chain check, plan, each archive part, staging synced, journal, post-check, tree check, `phase done` of short phases |

### Progress of long phases

Long phases log a `progress` line every 30 seconds (`--progress-interval SECONDS`,
before or after the command; `0` turns it off). Phases: `staging` (extracting the
archive), `installing` (deletes and renames into the tree), `post-check` (touched
paths), `tree scan` (post-check of a full export, tree check, `check`, `adopt`),
`content hash` (`--full-check`, `check --hash`, `adopt --hash`). Each line carries
`files`, `files_total`, for staging and hashing also `bytes`, `bytes_total` and
`rate_mib_s`, plus `percent`, `elapsed_s` and `eta_s`:

    {"event": "progress", "phase": "staging", "slug": "ubuntu", "files": 120000,
     "files_total": 480000, "bytes": 51539607552, "bytes_total": 214748364800,
     "percent": 24.0, "rate_mib_s": 81.9, "elapsed_s": 600, "eta_s": 1900, ...}

A phase that logged progress ends with `phase done` (files, bytes, duration, rate)
at info level; a phase shorter than the interval logs it at debug level only. For a
tree scan the total is the number of entries in `files.json`, so `percent` is an
estimate if the tree has extra paths.

## Behaviour

1. The checks of `mirrorhub.verify.verify()` run first, except hashing the archive
   parts: every other file in `MANIFEST.sha256` (`files.json`, `manifest.json`,
   `deleted.txt`) is hashed, the parts must exist and be listed. Then extra checks:
   slug and export id match the directory, contiguous part numbering, symlink targets,
   path conflicts. With `--verify-first` the unmodified `verify()` runs and hashes the
   parts up front as well (reads the archive twice).
2. Optional `gpgv` check against a keyring from the config; never from the export.
3. Per-slug lock, state file outside the mirror tree, chain check (base must equal the
   last applied export; a full export over existing data needs `--rebase`).
4. The archive is streamed (each part hashed against `MANIFEST.sha256` while reading,
   optional external `zstd`), every member is checked by hand, written to a staging
   directory and its size and SHA-256 compared with `files.json`. A damaged part fails
   at its end; if extraction fails for another reason, the parts are hashed to report
   a damaged archive as a verification failure (exit code 3). Either way staging is
   discarded and the tree is not touched.
5. The staging file system is synced once (`syncfs`, else `sync`), then a journal is
   written, deletions run, files are moved with `os.replace`, the tree is checked
   against `files.json` (post-check), then the state is updated. An interrupted run is
   continued by running the same import again.
6. A copy of the export's `files.json` is kept in `<state_dir>/inventories/<slug>/`
   (only the one for the current state), so `check` works after the export has left
   the inbox.
7. Quick comparison of the whole tree with `files.json` (see below); skip it with
   `--no-tree-check`.

### Post-check and how often data is read

Every byte is hashed once on its way into staging. The post-check therefore compares
file system metadata only: type, size, mtime (nanoseconds) and symlink targets of the
touched paths (incremental export) or of the whole tree, including extra paths (full
export). `--full-check` additionally reads the whole tree back and hashes it, which
also finds damage between writing and reading (disk, controller, file system); the
same check is available at any time as `check --hash`.

| | Archive read | Tree read back |
|---|---|---|
| default | once | no (metadata only) |
| `--verify-first` | twice | no |
| `--full-check` | once | once, whole tree |

Before this change (importer builds up to `7a3f3b2`) the archive was read twice,
every file was flushed to disk on its own (`fsync`) and a full export always re-read
and hashed the whole tree.

`scripts/bench_importer.py` measures import time, fsync calls and bytes read on
synthetic exports, also for two versions side by side (`--src before=… --src after=…`,
see the script's help). Run it on the receiver's disk type: an fsync costs about
0.5 ms on an SSD and 5–20 ms on a hard disk or USB medium.

The export directory is never modified. File modes are 0644 as written by the sender.

## Detecting changes in the tree

After an incremental import, the post-check covers only the paths the export
touched, and the sender never resends files that did not change. A file deleted or
damaged by hand on the receiver would otherwise go unnoticed. Two checks find it;
neither needs a return path to the sender:

- **After every import** the importer compares the whole tree with the applied
  `files.json`: every path present, same type, size, mtime (nanoseconds) and symlink
  target, no extra paths. No contents are read. The export is applied and the state
  updated anyway; differences are printed and logged ("tree differs from
  inventory"), and the run ends with exit code 11. With `--slug` or `--all` the
  comparison runs once per repository, after the last pending export, not after
  each one. If the last applied export was a full export, or the import used
  `--full-check`, its post-check already compared everything and the step is skipped.
- **`check`** runs the same comparison on demand against the inventory of the last
  applied export; `--hash` also compares SHA-256 of every file the quick check did
  not already report, and finds silent corruption with unchanged size and mtime. It
  takes the repository lock, changes nothing and exits with 0 (match) or 11
  (differences). `--all` checks every repository with a state.

The console shows the first 50 differences. For the complete lists, use
`check --output-dir DIR` (or `import --drift-output DIR`): it writes one file per
kind into `DIR/<slug>/`, one path per line, sorted, always all files (empty when
there is nothing of that kind), replacing the files of an earlier run:

| File | Meaning |
| --- | --- |
| `missing.txt` | listed in files.json, not in the tree |
| `extra.txt` | in the tree, not in files.json |
| `type.txt` | file where a symlink is expected, or the other way round |
| `size.txt` | different size |
| `mtime.txt` | same size, different mtime |
| `symlink-target.txt` | symlink points elsewhere |
| `sha256.txt` | different content (only with `--hash`) |
| `summary.txt` | `slug=`, `export_id=`, `checked_at=`, `check=quick` or `quick+sha256`, `total=` and one count per file |

A path containing a control character (a line break in a file name, only possible
for extra files) is written with backslash escapes. Examples:

    wc -l reports/ubuntu/*.txt                         # how much differs, per kind
    grep '^total=' reports/*/summary.txt               # per repository
    xargs -d '\n' -a reports/ubuntu/extra.txt -I{} echo "/srv/mirror/ubuntu/{}"

Run `check` regularly and let exit code 11 alert your monitoring, for example with
a weekly systemd timer:

    # /etc/systemd/system/mirrorhub-check.service
    [Service]
    Type=oneshot
    ExecStart=/usr/bin/python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml check --all

    # /etc/systemd/system/mirrorhub-check.timer
    [Timer]
    OnCalendar=weekly
    Persistent=true
    [Install]
    WantedBy=timers.target

and a monthly run with `--hash`. To repair, import a full export with `--rebase`
(a repair export that transfers only the differences is planned in #43).

## Superseded exports

The sender can build an incremental export on an earlier base ("incremental since"
on its Exports page), for example when an export was lost on the way. The exports
published between that base and the new export are then superseded:

- `import --slug` skips an export when a later pending export starts at the
  current state (its base is the last applied export).
- Any unapplied export whose id (a UTC timestamp) is older than the last applied
  export is skipped, also with `import --export`. A full export applied
  explicitly with `--rebase` is the exception.

A skipped export is recorded in the history with `"status": "superseded"`, logged
as a warning and never applied later; the run ends with exit code 0. `status`
shows the counts of applied and superseded exports.

## Lost state file: adopt

If the state file of a repository is lost but the mirror tree is intact,
`adopt` writes it for the export the tree matches, without any transfer:

- `--export-id <id>`: that export in the inbox; `--auto`: the published exports in
  the inbox, newest first, the first exact match wins.
- Only `files.json`, `manifest.json` and `MANIFEST.sha256` (plus the signature, if a
  keyring is configured) are needed; the archive may already be gone.
- Default quick check: every path present, same type, size, mtime (nanoseconds) and
  symlink target, no extra paths. No file contents are read, so it stays fast on
  large trees; it relies on the importer setting every mtime from `files.json`.
- `--hash` also compares SHA-256 and reads every file once.
- An exact match writes the state with that export as last applied (history entry
  `"status": "adopted"`). Differences abort with exit code 10 and list the first
  ten; nothing is written. An existing state is refused (exit code 5), as is an
  unfinished import in the staging directory.

## Exit codes

0 ok or no-op, 2 usage/config, 3 verification, 4 signature, 5 chain/base/gap,
6 unsafe archive, 7 lock held, 8 tool missing (zstd, gpgv), 9 I/O or resume needed,
10 post-check failed, 11 tree differs from the inventory (after an import: the export
was applied anyway).
