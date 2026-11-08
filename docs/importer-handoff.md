# Importer handoff

Shared exchange file between the MirrorHub sender-side session and the receiver-side importer session. Keep it factual; update the status section after each work step.

## Prompt for the importer session

### Role

Develop the receiver-side importer for MirrorHub (Python 3.12). MirrorHub is the sender: it builds offline APT/Arch mirrors and writes export packages into a drop folder; an external data-diode tool transfers them to an air-gapped network. The importer applies those exports to a local mirror tree. It does not exist yet.

### Assumptions (confirm before coding)

- Standalone, stdlib-only CLI, runnable with plain Python 3.12 (like `mirrorhub.verify`). No FastAPI/SQLModel/DB dependency.
- Target is a filesystem mirror tree per repository slug.
- zstd decompression may need the external `zstd` binary; fail clearly if missing.

### Export format

Authoritative sources: `docs/export-contract.md`, `src/mirrorhub/verify.py`, `src/mirrorhub/export/job.py`.

Location: `EXPORT_DIR/<repo-slug>/<YYYYMMDDThhmmssZ>-<full|incr>/`

Files (flat directory, no subdirectories, no symlinks):

- `data.tar` or `data.tar.zst`, optionally split into `.00000`, `.00001`, ... parts. Reassemble in numeric order before decompression/extraction.
- `MANIFEST.sha256`: lines `<64-hex sha256>  <filename>` (two spaces); covers every file except `.done`, `MANIFEST.sha256`, `MANIFEST.sha256.sig`.
- `manifest.json`: `format_version` == 1, `mode` (`full`|`incr`), `export_id`, `repo`, `base_export_id` (null for full, required for incr), `created_at`, `archive_format`, `archive_parts`, `payload_paths`, `counts` {inventory, payload, deleted}, `sizes`, `tool_versions`.
- `files.json`: complete inventory, `relative_path -> {type: file|symlink, size, mtime_ns, sha256}`; symlinks also carry `target`.
- `deleted.txt`: one removed relative path per line.
- `MANIFEST.sha256.sig`: optional detached GPG signature over `MANIFEST.sha256`.
- `.done`: written last; content equals `export_id`. Ignore `*.tmp` directories and directories without `.done`.

Relative paths: non-empty, no leading `/`, no backslash, no empty/`.`/`..` segments, no control characters.

### Semantics

- Full: no base, payload equals the complete inventory, no deletions.
- Incremental: payload holds new or changed entries only; removed paths are in `deleted.txt` and never in the inventory. Apply chains in base order (`base_export_id` -> `export_id`).
- A path counts as changed if its whole inventory entry (type, size, mtime_ns, sha256, target) differs from the previous one.
- After applying an export, the resulting tree must match `files.json`.
- Deduplicate by (repo slug, export_id). Importing the same export twice must be a no-op.

### Archive details

- `data.tar` is written with `tarfile.PAX_FORMAT`; optional zstd via the external binary; optional byte-wise split.
- Member names are the plain relative paths (no prefix directory).
- Members are regular files (mode 0644) and symlinks (SYMTYPE, mode 0777, linkname = relative target). uid/gid 0. No directories, hardlinks or device files; create parent directories yourself.
- Tar mtime has second resolution only. The exact `mtime_ns` exists only in `files.json`; set it with `os.utime(ns=...)` if the inventory must be reproduced exactly.
- Symlink inventory `sha256` is the sha256 of the target string.
- The sender inventory skips directories named `.temp`, `.tmp`, `.mirrorhub`, `.rsync-partial`.

### Requirements

1. Run `mirrorhub.verify.verify()` first (reuse or vendor it); refuse on any `VerificationError`.
2. Optional signature check: `gpgv --keyring <trusted keyring> MANIFEST.sha256.sig MANIFEST.sha256`. The trusted key is provisioned independently, never taken from the export.
3. Keep receiver state per slug outside the mirror tree (last applied export_id, history). Reject an incremental whose base does not equal the last applied export; reject gaps; accept a full over existing state only with an explicit `--rebase`.
4. Safe extraction: stream the tar; reject absolute paths, `..`, backslashes, special files, hardlinks and symlinks leaving the root; verify sha256 and size of every extracted file against `files.json`; never use `extractall` without `filter="data"` or an own member check.
5. Atomic apply: stage on the same filesystem, validate, then replace; apply `deleted.txt` only after successful extraction; resumable or cleanly reversible; per-repository lock.
6. Dry-run mode (verify and print planned adds/changes/deletes). Clear exit codes and structured logging.
7. Never modify or delete the export directory.
8. pytest coverage: full, incremental chain, deletions, tampered archive/checksum, path-traversal tar member, missing `.done`, wrong base, split parts, zst, idempotent re-import. Code must pass ruff and strict mypy (`ruff.toml`, `pyproject.toml`, `scripts/check.sh`).

### Repository conventions

- Package `src/mirrorhub/`; suggested module `src/mirrorhub/importer/` (`cli.py`, `state.py`, `apply.py`), tests in `tests/`.
- Initialization files are named exactly `__init__.py`.
- Follow `docs/release-checklist.md`. Do not weaken the verifier. Do not claim production readiness.
- Use a new feature branch, small commits; run `sh scripts/check.sh` and `python3 -m pytest -q` before committing.

### First steps

1. Read `README.md`, `docs/export-contract.md`, `src/mirrorhub/verify.py`, `src/mirrorhub/export/job.py`, `pack.py`, `manifest.py`.
2. Build fixtures for a full and an incremental export (real writer or hand-built) and develop against them.
3. Report open questions before implementing.

## Session exchange

Update this section after each step. Newest entry on top.

| Date | Session | Status | Notes |
| --- | --- | --- | --- |
| 2026-11-08 | sender | Handoff created | Prompt and format description written. Importer not started. |

### Open questions

- Where is the receiver mirror tree located and which layout (per slug) should the importer use?
- Is GPG signature verification mandatory on the receiver?
- Which pickup tool delivers exports, and does it preserve file names and `.done`?

### Known issues on the sender side

- `src/mirrorhub/export/_init_.py` has a single underscore on GitHub; it should be `__init__.py`.
- `src/mirrorhub/export/publish.py` is empty; publication logic lives in `job.py`.
