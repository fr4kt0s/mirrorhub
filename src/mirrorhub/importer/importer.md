# MirrorHub Receiver: Importer and Audit Guide

Status: experimental. Validate real exports, filesystem behavior, interrupted operations,
and recovery on the intended receiver before production use. Passing unit tests alone
is not a production-readiness guarantee.

## 1. Overview

MirrorHub runs on the sender side and produces offline mirror export packages. An
external data-diode tool transfers those packages to the receiver inbox. The importer
applies exports to a local mirror tree, one directory per repository slug.

The standalone audit script checks an existing mirror against an export inventory
and/or checks whether packages referenced by APT indexes exist.

Neither program downloads missing packages. The audit script does not repair mirrors.

### Example layout

```text
/srv/mirrorhub/
├── inbox/
│   ├── ansible/
│   │   ├── 20261107T185852Z-full/
│   │   └── 20261108T171030Z-incr/
│   └── ubuntu/
│       └── <export-id>/
└── mirror/
    ├── ansible/
    │   ├── dists/
    │   └── pool/
    ├── ubuntu/
    │   ├── dists/
    │   └── pool/
    └── .mirrorhub-import/

/var/lib/mirrorhub-importer/
├── ansible.json
├── ansible.lock
├── ubuntu.json
└── ubuntu.lock
```

Terminology:

| Term | Meaning |
| --- | --- |
| Mirror root | Parent directory of all receiver repository trees, such as `/srv/mirrorhub/mirror`. |
| Slug | Repository identifier and directory name, such as `ansible`. |
| Repository tree | One mirror, such as `/srv/mirrorhub/mirror/ansible`. |
| Inbox | Transferred export packages under `<export_dir>/<slug>/<export-id>/`. |
| State | Per-slug record of the last applied export and applied history. |
| Staging | Temporary payload and journal used during application or recovery. |

## 2. Export semantics

- A full export contains the complete payload and has no base export.
- An incremental export contains only new/changed entries and a deletion list. Its base
  must equal the last export applied to that slug.
- `files.json` always describes the complete resulting inventory, including in incrementals.
- Only exports with `.done` are published. Incomplete and `.tmp` directories are ignored
  by inbox discovery.
- Already applied export identifiers are no-ops, even with `--rebase`.
- Export directories are inputs: do not modify their files, checksums, identifiers or markers.

## 3. Setup

Use Python 3.12. Importing `.tar.zst` exports also requires the external `zstd` executable.
Optional signature verification requires `gpgv` and an independently provisioned trusted
public keyring. Never establish trust using a key delivered only with the export.

From the repository root:

```sh
cp importer.example.toml importer.toml
```

Edit the receiver paths:

```toml
export_dir = "/srv/mirrorhub/inbox"
mirror_root = "/srv/mirrorhub/mirror"
state_dir = "/var/lib/mirrorhub-importer"

# Optional; default is <mirror_root>/.mirrorhub-import.
# Staging and the repository target must be on the same filesystem.
# staging_dir = "/srv/mirrorhub/mirror/.mirrorhub-import"

# Optional independently provisioned keyring.
# keyring = "/etc/mirrorhub/export-signers.gpg"
# require_signature = false
```

Relative paths are resolved relative to the configuration file. Keep state outside the
mirror and inbox. Staging must not be inside an individual repository tree. Exclude it
from the web server if it is under the served mirror root. Protect local configuration
and do not accidentally commit deployment-specific settings.

The configuration path is selected with `--config`, or with the environment variable
`MIRRORHUB_IMPORTER_CONFIG`. Commands below assume the package is available to Python
and the working directory is the repository root.

## 4. Importing exports

### Plan one repository without applying changes

```sh
python3 -m mirrorhub.importer --config importer.toml import \
  --slug ansible --dry-run
```

The plan uses `A` for additions, `C` for changes and `D` for deletions. Dry-run checks
package checksums/metadata and the chain, but does not unpack or validate tar members.
It makes no receiver writes. Inbox mode plans only the first pending export per slug,
because later plans depend on application of their predecessors.

### Apply pending exports for one slug

```sh
python3 -m mirrorhub.importer --config importer.toml import --slug ansible
```

### Import all slugs

```sh
python3 -m mirrorhub.importer --config importer.toml import --all
```

Repositories are processed sequentially. A failed slug does not stop the others. The
highest per-slug error code is returned. This is not a severity ranking.

```sh
# Plan the first pending export of EACH slug.
python3 -m mirrorhub.importer --config importer.toml import --all --dry-run
```

### Apply one explicit export

```sh
python3 -m mirrorhub.importer --config importer.toml import \
  --export /srv/mirrorhub/inbox/ansible/20261108T171030Z-incr
```

### Show state

```sh
python3 -m mirrorhub.importer --config importer.toml status
```

### Important options

| Option | Behavior |
| --- | --- |
| `--dry-run` | Verification and planning only; no tar extraction and no receiver writes. |
| `--full-check` | Compare the whole tree after application; full exports already request this. Incrementals normally check touched paths. |
| `--rebase` | Explicitly allow a full export over existing content/state; remove entries absent from its inventory, subject to skipped directories. |
| `--discard-staging` | Allow discarding another export's unfinished journal; does not undo partial changes. |

`--rebase` and `--discard-staging` are refused with importer `--all`.

The importer verifies first, stages the payload, writes a journal, applies deletions and
per-file replacements, checks the result, and updates state last. A per-slug lock prevents
imports using the same state location from running concurrently. Per-file replacement
is not an atomic switch of the entire repository tree.

### Logs

Repository-specific JSON events include `slug`, for example:

```json
{"event": "applied", "export_id": "20261108T171030Z-incr", "level": "info", "slug": "ansible", "ts": "2026-11-08T17:18:33+00:00"}
```

JSON events go to stderr; human-readable summaries go to stdout. Global configuration
errors may have no slug.

## 5. Inventory audit

The standalone script is `scripts/audit_tree.py`. It does not read `importer.toml`.
Supply explicit paths. Use importer `status` to identify the last applied export:

```sh
python3 -m mirrorhub.importer --config importer.toml status

python3 scripts/audit_tree.py \
  /srv/mirrorhub/inbox/ansible/20261108T171030Z-incr \
  /srv/mirrorhub/mirror/ansible \
  --log-dir ./audit-logs
```

Use the last applied export, not necessarily the newest one present in the inbox. An old
full export will legitimately differ after newer incrementals have been applied.

| Terminal label | Meaning |
| --- | --- |
| `MISSING` | Inventory path is absent from the tree. |
| `DIFFERS` | Type, size, file SHA-256 or symlink target differs. |
| `EXTRA` | A tree entry is absent from the inventory. |
| `UNSAFE` | A checked path would traverse outside the mirror. |

The inventory audit does not compare mtimes, permissions or ownership. It does not
verify export signatures or substitute for `mirrorhub.verify`. Use trusted, verified
metadata. Stop imports and other writers while auditing to avoid inconsistent results.

Skipped directory names in the current audit script are `.temp`, `.tmp`, `.mirrorhub`,
`.rsync-partial` and `.mirrorhub-import`.

## 6. APT package audit

A mirror may match its exported inventory while APT indexes reference packages absent
from that inventory. The APT check catches those missing references independently.

### One slug, no export required

```sh
python3 scripts/audit_tree.py \
  /srv/mirrorhub/mirror/ansible \
  --apt-only --log-dir ./audit-logs
```

### Combine inventory and APT checks

```sh
python3 scripts/audit_tree.py \
  /srv/mirrorhub/inbox/ansible/20261108T171030Z-incr \
  /srv/mirrorhub/mirror/ansible \
  --apt --log-dir ./audit-logs
```

### Also compare package SHA-256

```sh
python3 scripts/audit_tree.py \
  /srv/mirrorhub/mirror/ansible \
  --apt-only --apt-hash --log-dir ./audit-logs
```

`--apt-hash` also enables APT checks when used with the two-path inventory invocation.

### What is checked

Below `dists/`, one index is selected per directory: `Packages.gz`, otherwise
`Packages.xz`, otherwise plain `Packages`. The different compression variants are not
compared with one another. `Filename` is resolved relative to the individual slug tree.

The script checks existence and declared size. `--apt-hash` additionally compares SHA-256
when supplied. Missing optional metadata is not itself an error. Identical filename,
size and hash declarations are checked once; conflicting declarations separately.

| Terminal label | Meaning |
| --- | --- |
| `APT-MISSING` | Referenced package file is absent. |
| `APT-SIZE` | File size differs from the index. |
| `APT-HASH` | SHA-256 differs (`--apt-hash`). |
| `APT-TYPE` | Referenced path exists but is not a regular file. |
| `APT-BADNAME` | Invalid/missing Filename or a target outside the mirror. |
| `APT-METADATA` | Size is present but not an ASCII nonnegative integer. |
| `APT-INDEX` | No index found, or an index could not be read/checked. |

This does not check Release/InRelease signatures, Sources indexes, source package
references, Arch databases or upstream availability. It is consistency checking, not
APT authentication or a package downloader.

## 7. Audit every APT slug with --all

Pass the parent mirror root, not an individual repository tree:

```sh
python3 scripts/audit_tree.py \
  /srv/mirrorhub/mirror \
  --all --log-dir ./audit-logs
```

The alias `-all` is also accepted:

```sh
python3 scripts/audit_tree.py \
  /srv/mirrorhub/mirror \
  -all --log-dir ./audit-logs
```

To compare package hashes in every discovered APT repository:

```sh
python3 scripts/audit_tree.py \
  /srv/mirrorhub/mirror \
  --all --apt-hash --log-dir ./audit-logs
```

Discovery is limited to direct child directories containing `dists/`:

```text
mirror/
├── ansible/dists/       -> checked as slug ansible
├── ubuntu/dists/        -> checked as slug ubuntu
├── debian/dists/        -> checked as slug debian
├── arch/                -> skipped: no dists/
├── .mirrorhub-import/   -> skipped
└── group/nested/dists/  -> not recursively discovered
```

Symlink repository directories are skipped. A child with `dists/` but no usable Packages
index is selected and reports a problem. A root containing no APT repositories returns
exit code 2. Reports must be outside the entire mirror root in this mode.

Audit `--all`:

- Implies APT-only checking: no export path or `files.json` comparison.
- Accepts exactly one positional path, the mirror root.
- Does not require `--apt-only` or `--apt`.
- Processes discovered slugs sequentially and continues after repository failures.
- Includes the slug in terminal messages and writes a separate report for every completed audit.
- Returns the highest repository exit code.

Example output:

```text
[ansible] starting audit: /srv/mirrorhub/mirror/ansible
[ansible] APT-MISSING 'pool/main/r/resolvelib/example.deb' (listed in dists/focal/main/binary-amd64/Packages.gz)
[ansible] missing-file report: audit-logs/ansible-missing.log (1 unique paths)
[ansible] 1 problem(s)
[ubuntu] starting audit: /srv/mirrorhub/mirror/ubuntu
[ubuntu] missing-file report: audit-logs/ubuntu-missing.log (0 unique paths)
[ubuntu] 0 problem(s)
ALL: 2 repositories, 1 clean, 1 with problems, 0 failed
```

The two `--all` options are different commands:

| Command | Purpose |
| --- | --- |
| `python3 -m mirrorhub.importer ... import --all` | Apply pending exports from the configured inbox. |
| `python3 scripts/audit_tree.py MIRROR_ROOT --all` | Check APT package references in existing mirror trees; do not import anything. |

## 8. Missing-file logs with slug prefixes

For `TREE_DIR=/srv/mirrorhub/mirror/ansible`, the slug is `ansible` and the report is
`<log-dir>/ansible-missing.log`.

Example report content:

```text
ansible/pool/main/a/ansible/ansible_5.10.0-1ppa~focal_all.deb
ansible/pool/main/r/resolvelib/python3-resolvelib_0.5.4-2ppa~focal_all.deb
```

Each report path is relative to the parent MIRROR ROOT and includes its slug:

```text
Mirror root: /srv/mirrorhub/mirror/
Log entry:   ansible/pool/main/r/resolvelib/python3-resolvelib_0.5.4-2ppa~focal_all.deb
Full path:   /srv/mirrorhub/mirror/ansible/pool/main/r/resolvelib/python3-resolvelib_0.5.4-2ppa~focal_all.deb
```

Do not prepend the slug twice. Internally, index Filename values and files.json keys
remain relative to the slug tree, without the slug prefix. Only report writing adds it.

### Report rules

- `--log-dir` defaults to the current working directory; missing parents are created.
- Logs must be outside the checked tree/export; with `--all`, outside the entire mirror root.
- One sorted, deduplicated, slug-prefixed path per line; no leading slash or extra log text.
- `MISSING` and `APT-MISSING` paths are combined. Other errors remain in terminal output.
- A completed report is atomically replaced, never appended. No missing paths means an empty file.
- A fatal error may leave an old report untouched: check exit status and terminal output.
- An unreadable APT index can yield an incomplete report from the remaining indexes, with exit 1.
- A zero-length report is not proof of a clean audit: hash errors, size errors and index failures
  do not create missing-file entries.
- Reports contain no timestamp or history. Copy them elsewhere for retention.
- Concurrent audits writing the same slug/report can overwrite one another. Use separate log
  directories or run sequentially.
- Reports for slugs skipped or no longer present are not automatically deleted; they may be stale.

After `--all`, you may have:

```text
audit-logs/
├── ansible-missing.log   # entries begin ansible/
├── ubuntu-missing.log    # entries begin ubuntu/
└── debian-missing.log    # entries begin debian/
```

Logs are diagnostic lists, not deletion lists or instructions to alter export metadata.

## 9. Repairing a repository

There is no dedicated repair command. First identify whether the fault is in the receiver
mirror, its state/chain, the transferred export, or the sender's repository snapshot.

### 9.1 Preserve evidence before repair

1. Stop scheduled/manual importers and other writers for the affected slug.
2. Pause serving if clients must not see mixed or partially repaired contents.
3. Record importer `status`, error messages, export id and missing-file reports.
4. Back up consistent mirror/state/staging together when practical; preserve a usable backup.
5. Correct disk-space, permission or storage problems before retrying.

Never invent base ids, weaken the verifier, edit checksums, or delete live lock files.
Do not reset state just to silence a chain error.

### 9.2 Interrupted import

The mirror may be partly updated while state still names the predecessor. A journal may
remain at `<staging_dir>/<slug>/journal.json`. Prefer retrying the exact export it names:

```sh
python3 -m mirrorhub.importer --config importer.toml import \
  --export /srv/mirrorhub/inbox/ansible/20261108T171030Z-incr
```

The importer attempts roll-forward, not rollback. Resume is not guaranteed for every
failure, especially path-type transitions. If resume fails, preserve the journal and
consider an isolated rebuild. Discarding staging never undoes changes already applied.

### 9.3 Wrong base or missing predecessor

Obtain the missing chain members and apply in base order. If the chain cannot be completed,
request a new full export. Never change `last_export_id` to pretend an export was applied.
Restore state only alongside its matching mirror tree.

### 9.4 Damaged receiver mirror: obtain a new full export

If the sender mirror is correct, request a NEW full export with a new export id. Use its
exact path rather than `--slug`, avoiding unrelated pending exports:

```sh
python3 -m mirrorhub.importer --config importer.toml import \
  --export /srv/mirrorhub/inbox/ansible/NEW_TIMESTAMP-full \
  --rebase --dry-run

python3 -m mirrorhub.importer --config importer.toml import \
  --export /srv/mirrorhub/inbox/ansible/NEW_TIMESTAMP-full \
  --rebase --full-check
```

Replace `NEW_TIMESTAMP-full` with the actual directory name. Rebase removes local entries
absent from the full inventory, subject to skipped directories. Inspect the deletion plan
and back up first. A journal for another unfinished export blocks the operation; prefer
finishing it, or use an isolated rebuild if in-place recovery is uncertain.

An already applied full id remains a no-op even with `--rebase`. Obtain a new export id
or use fresh recovery state. Subsequent incrementals must base on the chosen full export.

### 9.5 Isolated rebuild with separate state

Use this if tree/state consistency is uncertain, a full export was already applied, or
resume repeatedly fails. It requires space for another mirror tree.

1. Copy the config to `importer-recovery.toml`.
2. Keep the same inbox, but use new empty mirror and state locations. Explicitly configure
   separate staging on the recovery mirror filesystem; do not reuse unfinished staging.
3. Import a valid full export using `--export`. Fresh state and an empty tree need no rebase.
   Apply its dependent incrementals one by one in base order if needed.
4. Audit against the last applied inventory and run APT checks with `--apt-hash`.
5. Stop importers and serving. Switch to the recovered mirror AND its matching state/config
   together, checking permissions and paths before restarting.
6. Preserve old mirror/state/staging for rollback until recovery is confirmed.

Never run importers with separate state/lock directories against the same target tree:
their locks would not coordinate writes. Do not pair a rebuilt tree with old state.

### 9.6 APT missing packages while the inventory audit passes

The receiver may match its export exactly while indexes reference packages the sender
did not include. Re-importing that export cannot supply files absent from its inventory.

1. Find the path in the last export's files.json. Remove the slug prefix from the report
   entry when looking up inventory keys or APT Filename fields.
2. Check sender architecture/component/source selections and upstream snapshot consistency.
3. Correct the sender mirror, complete a successful sync and generate a new full export.
4. Transfer, import and audit again. Do not fabricate packages or edit signed metadata.

Example mapping:

```text
Log entry:
ansible/pool/main/r/resolvelib/python3-resolvelib_0.5.4-2ppa~focal_all.deb

files.json key / APT Filename:
pool/main/r/resolvelib/python3-resolvelib_0.5.4-2ppa~focal_all.deb
```

### 9.7 Other failures

- Post-check failure leaves state unadvanced. In the current implementation that failure
  path removes staging; preserve output and investigate the named path. The importer checks
  nanosecond mtimes while the standalone audit does not, so audit can pass despite an mtime
  post-check failure.
- Damaged or unsafe export: preserve it and obtain a correct retransmission through the
  transfer process or a new export. Do not edit published files or checksum manifests.
- Invalid/lost state: restore matching tree/state backups or rebuild in isolation.
- Lock held: wait or investigate the process. A leftover `.lock` filename alone does not
  prove a lock is held. Never delete it while an import is running.
- Missing zstd/gpgv: provision the executable and retry after checking state/staging.

## 10. Exit codes

### Importer

| Code | Meaning |
| --- | --- |
| 0 | Success or already applied/no-op. |
| 2 | Usage/configuration error. |
| 3 | Verification failure. |
| 4 | Signature failure. |
| 5 | Chain/base error. |
| 6 | Unsafe/inconsistent archive. |
| 7 | Lock held. |
| 8 | External tool missing. |
| 9 | I/O failure or unfinished import needs attention. |
| 10 | Post-check failure. |

### Audit script

| Code | Meaning |
| --- | --- |
| 0 | No problems found by the selected checks. |
| 1 | Differences, missing files, unsafe paths or APT-index errors. |
| 2 | Usage/fatal input/I/O error; or --all found no APT repositories. Report may not be refreshed. |

Audit `--all` returns the highest per-repository code. Check both the per-slug summary and
its overall summary; do not infer health only from whether missing-file logs are empty.

## 11. Help and development checks

```sh
python3 scripts/audit_tree.py --help
python3 -m mirrorhub.importer --help

python3 -m ruff check scripts/audit_tree.py src/mirrorhub/importer tests/test_importer.py
python3 -m ruff format --check scripts/audit_tree.py src/mirrorhub/importer tests/test_importer.py
python3 -m mypy src/mirrorhub/importer tests/test_importer.py
python3 -m pytest -q tests/test_importer.py

# Whole-repository release checks; do not bypass unrelated failures.
sh scripts/check.sh
python3 -m pytest -q
```

Test real exports, zstd, split archives, path-type changes, interrupted application and
storage failures on the actual receiver. Keep serving and importer execution separate.