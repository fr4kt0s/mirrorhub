# MirrorHub importer (receiver side)

> **Status: experimental.** The importer has only been tested against hand-built fixtures that follow
> `docs/export-contract.md`. It has not been validated against the real export writer, a real data
> diode, or large mirrors. Do not treat it as production-ready.

The importer runs on the air-gapped side. It applies MirrorHub export packages (full and incremental)
to a local mirror tree, one tree per repository slug. It uses only the Python 3.12 standard library
plus the external `zstd` binary (for `.tar.zst` exports) and `gpgv` (only for signature checks).

## 1. Concepts

| Term | Meaning |
| --- | --- |
| Slug | Repository id. Inbox folder, mirror tree, state file and lock are all per slug. |
| Export | Directory `<export_dir>/<slug>/<YYYYMMDDThhmmssZ>-<full\|incr>/` containing the archive, metadata and `.done`. |
| Full export | Complete inventory of the repository. No base. |
| Incremental export | Only new or changed entries plus `deleted.txt`. Its `base_export_id` must be the last export applied. |
| State | `<state_dir>/<slug>.json`: last applied export id and the history. Lives outside the mirror tree. |
| Staging | `<staging_dir>/<slug>/` with `journal.json` and `files/`. Exists only while an import is running or was interrupted. |
| Inbox | `export_dir`, where the data-diode pickup tool puts the exports. The importer only reads it. |

The importer never modifies or deletes anything in the export directory.

## 2. Setup

1. Copy `importer.example.toml` to `importer.toml` and edit it. Relative paths are relative to the config file.

   ```toml
   export_dir  = "/srv/mirrorhub/inbox"            # inbox: <export_dir>/<slug>/<export-id>/
   mirror_root = "/srv/mirrorhub/mirror"           # mirror tree: <mirror_root>/<slug>/
   state_dir   = "/var/lib/mirrorhub-importer"     # state + lock files, OUTSIDE the mirror tree
   # staging_dir = "/srv/mirrorhub/mirror/.mirrorhub-import"   # default; same filesystem as mirror_root
   # keyring = "/etc/mirrorhub/export-signers.gpg"             # optional, trusted public keys
   # require_signature = false
   ```

2. Run from the repository root (or install the package) with Python 3.12:

   ```sh
   python3 -m mirrorhub.importer --config importer.toml import --slug ubuntu
   ```

   The config path can also be set with `MIRRORHUB_IMPORTER_CONFIG`.

Notes:

- `staging_dir` must be on the same filesystem as `mirror_root` and must not be inside a repository tree.
  The default `<mirror_root>/.mirrorhub-import` is below the mirror root, so exclude it from your web server.
- `zstd` must be installed for `.tar.zst` exports. The importer stops with exit code 8 if it is missing.
- Keep `state_dir` out of the mirror tree and back it up.

## 3. Everyday use

```sh
# Plan only (no writes). Shows A(dded) / C(hanged) / D(eleted) paths for the first pending export.
python3 -m mirrorhub.importer --config importer.toml import --slug ubuntu --dry-run

# Apply all pending exports of one repository, oldest first.
python3 -m mirrorhub.importer --config importer.toml import --slug ubuntu

# Apply every repository found in the inbox. One failing slug does not stop the others.
python3 -m mirrorhub.importer --config importer.toml import --all

# Apply one specific export directory (slug is taken from its parent folder).
python3 -m mirrorhub.importer --config importer.toml import --export /srv/mirrorhub/inbox/ubuntu/20261101T000000Z-full

# Show the receiver state.
python3 -m mirrorhub.importer --config importer.toml status
```

Options:

| Option | Effect |
| --- | --- |
| `--dry-run` | Verify and print the plan. Writes nothing. It hashes the archive parts during verification but does not unpack the archive. With `--slug`/`--all` it plans only the first pending export of each slug, because later plans depend on the earlier ones. |
| `--rebase` | Allow a full export over existing state or existing files. Deletes everything in the slug tree that is not in the new inventory. Not allowed with `--all`. |
| `--full-check` | After applying, hash the whole tree and compare it with `files.json` (always done for full exports). |
| `--discard-staging` | Drop an unfinished import of a *different* export. Not allowed with `--all`. |
| `--log-level` | `debug`, `info`, `warning`, `error`. |

Logs are JSON lines on stderr. Every event that belongs to a repository carries a `slug` field:

```json
{"event": "applied", "export_id": "20261107T185852Z-full", "level": "info", "slug": "ubuntu", "ts": "2026-11-08T17:18:33+00:00"}
```

## 4. What happens during an import

1. `mirrorhub.verify.verify()` runs first (checksums, metadata, `.done`). Any failure stops the import. Additional checks follow: slug and export id match the directory, archive parts are numbered `00000…` without gaps, symlink targets stay inside the repository, no path conflicts.
2. Optional signature check with `gpgv` against the configured keyring. The key must be provisioned independently, never taken from the export.
3. A per-slug lock is taken. Already applied exports are a no-op.
4. Chain check: an incremental export must have `base_export_id` equal to the last applied export. A full export over existing state or a non-empty tree needs `--rebase`.
5. The archive is streamed (parts are re-hashed while reading). Every tar member is checked and written to staging. Only regular files and symlinks listed in the payload are accepted; absolute paths, `..`, backslashes, hardlinks and special files are rejected.
6. A journal is written, `deleted.txt` entries are removed, and files are moved into place one by one. Empty parent directories of deleted files are removed.
7. Post-check: touched paths (or the whole tree for full exports or `--full-check`) must match `files.json` in type, size, sha256 and mtime.
8. The state file is updated and the staging directory is removed.

The state is updated last. If a run is interrupted before that, running the same import again continues from the journal.

## 5. Exit codes

| Code | Meaning | Typical action |
| --- | --- | --- |
| 0 | Success or no-op | None. |
| 2 | Usage or config error | Fix the command or `importer.toml`. |
| 3 | Verification failed | The export is damaged or incomplete. See 6.6. |
| 4 | Signature check failed | Check the keyring and the export. |
| 5 | Chain, base or gap error | See 6.3. |
| 6 | Unsafe or inconsistent archive | Do not use this export. See 6.6. |
| 7 | Lock held | Another import of the same slug is running. See 6.8. |
| 8 | Tool missing (`zstd`, `gpgv`) | Install it. |
| 9 | I/O error or resume needed | See 6.1, 6.2 and 6.7. |
| 10 | Post-check failed | See 6.5. |

With `--all` the exit code is the highest code of any failed slug.

## 6. Repairing a repository

The importer has no separate "repair" command. Repairs use three tools: re-running an import
(it resumes), a full export together with `--rebase`, and, as a last resort, resetting the state file
by hand. Pick the situation that matches the symptom.

### Before you touch anything

- Never edit, move or delete anything inside the export directories. If an export is bad, ask for it to be transferred again.
- Stop other importers for the same slug (cron jobs, other terminals).
- Back up the state file: `cp <state_dir>/<slug>.json <state_dir>/<slug>.json.bak`.
- Look at the last log lines and `status` first:

  ```sh
  python3 -m mirrorhub.importer --config importer.toml status
  ```

### 6.1 An import was interrupted (crash, power loss, Ctrl-C)

Symptom: the state still shows the old export, and `<staging_dir>/<slug>/journal.json` exists. The
mirror tree may be partly updated.

Fix: run the same import again.

```sh
python3 -m mirrorhub.importer --config importer.toml import --slug ubuntu
```

The log shows `resuming interrupted import`. The remaining steps are idempotent, so nothing is lost.
Do not delete the staging directory by hand.

### 6.2 "staging holds an unfinished import of …" (exit 9)

Staging contains a journal for a different export than the one you are importing.

1. Preferred: import the export named in the message first. It resumes and finishes (see 6.1). Then import the others.
2. Only if that export is no longer available, run with `--discard-staging`. The tree may then be a mix of two versions, so afterwards repair the tree as in 6.4.

### 6.3 Chain errors: "base mismatch", "no export has been applied yet", gap (exit 5)

The incremental export does not build on what the receiver has.

- **A middle export is missing.** Get the missing export(s) from the sender, put them into the inbox, and run the import again. Exports are applied oldest first.
- **The state is behind or ahead of the tree** (for example, a restored backup). Compare `status` with the exports in the inbox. If the chain cannot be completed, rebuild from a full export as in 6.4.
- **Older export in the inbox that was never applied.** It is skipped with a warning (`skipping older unapplied export`). It cannot be applied after a newer one.

Never force an incremental export onto a different base.

### 6.4 The mirror tree is damaged, modified or has stray files

Symptoms: files were deleted or changed by hand, a disk problem, `--discard-staging` was used, a
post-check keeps failing, or the state and the tree no longer agree.

First, find out what is wrong without changing anything. The importer has no standalone check, so
use the read-only audit script from section 7 with the `files.json` of the last applied export:

```sh
python3 audit_tree.py /srv/mirrorhub/inbox/ubuntu/<last-export-id> /srv/mirrorhub/mirror/ubuntu
```

It prints `MISSING`, `DIFFERS` and `EXTRA` paths. Then rebuild the tree from a full export.

**Option A: a new full export from the sender (recommended).**

1. Ask the sender side to produce a new full export and transfer it into the inbox.
2. Check the plan: `import --slug ubuntu --dry-run --rebase` (the `D` lines are files that will be deleted).
3. Apply it with rebase. Every file of a full export is rewritten, files that are not in its inventory are deleted, and the whole tree is checked afterwards.

   ```sh
   python3 -m mirrorhub.importer --config importer.toml import --slug ubuntu --rebase
   ```

   Later incremental exports of the new chain are applied normally.

A full export that was already applied cannot be applied again ("already applied", no-op). That is why a
new export is needed for option A.

**Option B: re-apply the existing chain (full export plus all later incrementals are still in the inbox).**

1. Make sure nothing else is importing this slug.
2. Move the state file away (keep the backup): `mv <state_dir>/ubuntu.json <state_dir>/ubuntu.json.old`
3. Run the import with `--rebase`. The first export (the full one) is applied over the existing tree, then the incrementals follow in order:

   ```sh
   python3 -m mirrorhub.importer --config importer.toml import --slug ubuntu --rebase
   ```

   `--rebase` applies only to the first export of the run; later ones are applied normally.
4. Run `status` and the audit script to confirm.

Option B requires that the full export and every later incremental are still present and valid in the
inbox. If a retention cleanup removed any of them, use option A.

Important:

- Rebase deletes every file in `<mirror_root>/<slug>` that is not in the full export's inventory, except the directories `.temp`, `.tmp`, `.mirrorhub` and `.rsync-partial`. Do not keep local files in that tree.
- Rebase is a deliberate decision per repository, so it is refused with `--all`.

### 6.5 Post-check failed (exit 10)

The files were applied, but the tree does not match `files.json` (for example a sha256 or mtime mismatch,
or paths that are not in the inventory). The state was not updated and the staging directory was removed.

1. Read the error message: it names the path.
2. Check the disk, filesystem and permissions of `mirror_root`. A mtime mismatch can mean the filesystem does not support setting nanosecond mtimes.
3. Run the same import again. It extracts and applies once more.
4. If it fails again with the same path, rebuild the tree as in 6.4.

### 6.6 Verification failed or unsafe archive (exit 3 or 6)

The export itself is damaged, incomplete or does not match its manifest. The importer has not changed
the mirror tree.

1. Check that the directory has a `.done` file and that the transfer finished.
2. Run the verifier on its own: `python3 -m mirrorhub.verify <export-directory>`.
3. Ask for the export to be transferred again, or for the sender to produce a new full export. Do not edit files in the export.
4. Do not disable checks to get past an error.

### 6.7 State file is invalid or lost (exit 9, "state file is invalid")

- Restore the backup of `<state_dir>/<slug>.json` if you have one. The tree and the state must belong together.
- Without a backup, treat it as a lost state: the first import then needs `--rebase` because the tree is not empty. Follow option B in 6.4.

### 6.8 "another import holds the lock" (exit 7)

Another importer process is running for this slug. Wait for it, or check with `ps`. The lock is released
automatically when the process ends, so a leftover `<slug>.lock` file is harmless. Do not delete it while an import is running.

### 6.9 Tool missing (exit 8)

Install `zstd` (for `.tar.zst`) or `gpgv` (for signature checks) and run the import again. Nothing was changed.

## 7. Audit script (read-only)

Save as `audit_tree.py`. It compares a mirror tree with the `files.json` of an export and changes nothing.
Exit code 0 means the tree matches.

```python
#!/usr/bin/env python3
"""Compare a mirror tree with the files.json of an export (read-only)."""
import hashlib
import json
import os
import sys
from pathlib import Path

SKIP = {".temp", ".tmp", ".mirrorhub", ".rsync-partial"}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


export, tree = Path(sys.argv[1]), Path(sys.argv[2])
inventory = json.loads((export / "files.json").read_text(encoding="utf-8"))
problems = 0
for rel, entry in sorted(inventory.items()):
    path = tree / rel
    if not os.path.lexists(path):
        print("MISSING ", rel)
        problems += 1
    elif entry["type"] == "symlink":
        if not (path.is_symlink() and os.readlink(path) == entry["target"]):
            print("DIFFERS ", rel)
            problems += 1
    elif path.is_symlink() or not path.is_file():
        print("DIFFERS ", rel)
        problems += 1
    elif path.stat().st_size != entry["size"] or digest(path) != entry["sha256"]:
        print("DIFFERS ", rel)
        problems += 1
present = set()
for directory, dirs, files in os.walk(tree):
    base = Path(directory)
    links = [n for n in dirs if (base / n).is_symlink()]
    dirs[:] = [n for n in dirs if n not in SKIP and n not in links]
    for name in [*files, *links]:
        present.add((base / name).relative_to(tree).as_posix())
for rel in sorted(present - set(inventory)):
    print("EXTRA   ", rel)
    problems += 1
print(f"{problems} problem(s)")
sys.exit(1 if problems else 0)
```

## 8. Limits

- Exports are applied one slug at a time and one after another. There is no parallel mode.
- Dry-run plans only the first pending export, and it does not unpack the archive. Problems inside the tar show up during a real import.
- File modes are `0644`, as written by the sender. Executable bits are not preserved.
- The importer cannot repair a repository from a damaged export. It needs a valid full export for that.
- There is no standalone integrity check of the mirror tree besides the post-check and the audit script above.