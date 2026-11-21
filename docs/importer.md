# MirrorHub importer (receiver side)

Status: experimental. Tested only against hand-built fixtures that follow
`export-contract.md`, not against the real writer or a real data diode.

## Usage

    python -m mirrorhub.importer --config importer.toml import --slug ubuntu
    python -m mirrorhub.importer --config importer.toml import --slug ubuntu --dry-run
    python -m mirrorhub.importer --config importer.toml import --export /inbox/ubuntu/<id>
    python -m mirrorhub.importer --config importer.toml status
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

The export directory is never modified. File modes are 0644 as written by the sender.

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
10 post-check failed.
