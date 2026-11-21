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

## Behaviour

1. `mirrorhub.verify.verify()` runs first, followed by extra checks: slug and export id
   match the directory, contiguous part numbering, symlink targets, path conflicts.
2. Optional `gpgv` check against a keyring from the config; never from the export.
3. Per-slug lock, state file outside the mirror tree, chain check (base must equal the
   last applied export; a full export over existing data needs `--rebase`).
4. The archive is streamed (parts re-hashed while reading, optional external `zstd`),
   every member is checked by hand and written to a staging directory.
5. A journal is written, deletions run, files are moved with `os.replace`, the tree is
   checked against `files.json`, then the state is updated. An interrupted run is
   continued by running the same import again.
6. A copy of the export's `files.json` is kept in `<state_dir>/inventories/<slug>/`
   (only the one for the current state), so `check` works after the export has left
   the inbox.
7. Quick comparison of the whole tree with `files.json` (see below); skip it with
   `--no-tree-check`.

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
  inventory"), and the run ends with exit code 11. A full export, or an import with
  `--full-check`, already compares everything and skips this step.
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
