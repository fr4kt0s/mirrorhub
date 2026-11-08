# MirrorHub importer (receiver side)

Status: experimental. Tested only against hand-built fixtures that follow
`export-contract.md`, not against the real writer or a real data diode.

## Usage

    python -m mirrorhub.importer --config importer.toml import --slug ubuntu
    python -m mirrorhub.importer --config importer.toml import --slug ubuntu --dry-run
    python -m mirrorhub.importer --config importer.toml import --export /inbox/ubuntu/<id>
    python -m mirrorhub.importer --config importer.toml status

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

## Exit codes

0 ok or no-op, 2 usage/config, 3 verification, 4 signature, 5 chain/base/gap,
6 unsafe archive, 7 lock held, 8 tool missing (zstd, gpgv), 9 I/O or resume needed,
10 post-check failed.
