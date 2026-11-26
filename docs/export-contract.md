# MirrorHub export contract

## Scope

MirrorHub writes exports into a configured drop folder. An external pickup tool
transfers them through the data diode.

MirrorHub does not implement the receiver, extraction, deletion application, or
air-gapped client configuration.

MirrorHub never automatically deletes published exports, failed export
directories, or abandoned temporary export directories.

## Directory layout

Each export is placed under:

    EXPORT_DIR/<repo-slug>/<YYYYMMDDThhmmssZ>-<full|incr>/

An export contains:

- data.tar or data.tar.zst, optionally split into numbered parts.
- MANIFEST.sha256, covering archive parts and JSON/text metadata files.
- manifest.json, describing the export identity, base and payload. Its
  `tool_versions` include the MirrorHub version and, when known, the sender's
  commit (`mirrorhub_commit`, 12 hex digits); receivers only compare it and warn.
- files.json, containing the complete repository inventory.
- deleted.txt, containing one removed relative path per line.
- MANIFEST.sha256.sig, if signing is configured.
- .done, written last.

Split part names use five-digit numeric suffixes:

    data.tar.00000
    data.tar.00001

For compressed archives, the names are:

    data.tar.zst.00000
    data.tar.zst.00001

Reassemble parts in numeric order before decompression or extraction.

## Pickup requirements

The pickup tool MUST:

1. Ignore directories ending in .tmp.
2. Ignore every directory without a .done marker.
3. Transfer every file of an export, including .done, without modifying it.
4. Transfer full exports before dependent incremental exports.
5. Preserve export identifiers and base identifiers.
6. Deduplicate by repository slug and export identifier.

The pickup tool MAY move published export directories out of the drop folder and
delete them later. Moving the whole `<slug>/<export-id>/` directory at once is
preferred.

MirrorHub does not need a published export after publication. Before writing
.done it stores a copy of the export's metadata (files.json, manifest.json,
MANIFEST.sha256, MANIFEST.sha256.sig, deleted.txt) on its state volume under
`export-meta/<slug>/<export-id>/`. After .done it writes a `.published` marker
into that copy (export id, created_at, mode, base, publication time). The next
incremental export reads its base inventory from there and checks files.json and
manifest.json against MANIFEST.sha256. Archive parts of the base are neither
required nor hashed.

The marked copies are the authority for which exports were published. The base
of the next incremental export is the newest marked copy that no other marked
copy uses as its base. The database export history is restored from the copies
when rows are missing. If the database lists a newer published export than the
copies and the drop folder no longer holds it, the export fails instead of
falling back to an older base. Copies that predate markers get one when the
database or the drop folder (.done) shows the export was published.

Exports published before this behaviour existed are read once from the drop
folder, and a copy is stored then. If neither the copy nor the directory exists,
the next incremental export fails and asks for a full export.

Drop-folder housekeeping (every scheduler tick, about 30 s):

- Empty export directories are removed.
- Export directories that contain only metadata files and .done, no archive
  part, and have not changed for 24 hours are removed. The grace period protects
  a pickup tool that moves files one by one.
- Empty repository directories are removed when no export of that repository is
  running.
- `<export-id>.tmp` directories and unexpected content are never touched.

Metadata copies are kept for the newest `MIRRORHUB_EXPORT_META_KEEP` exports per
repository (default 30), and always for the newest published one. Applying the retention rule also removes the copies of
the deleted exports.

The appearance of .done is the publication signal, not the mere existence of
the final directory.

## Publication ordering

MirrorHub writes into a sibling <export-id>.tmp directory.

It completes the archive, inventories, metadata, checksums and optional signature,
then fsyncs the files and temporary directory.

It renames the temporary directory to the final export directory and fsyncs the
parent directory.

It then creates and fsyncs .done and fsyncs the final directory.

The temporary and final directories must reside on the same filesystem.

The filesystem and NFS server must support the required rename and fsync
operations. MirrorHub does not silently ignore fsync failures.

Server-side durability depends on the NFS server and storage configuration.
Test the contract with the actual server before production use.

## Incremental bases

The first incremental request produces a full export.

Subsequent incremental exports compare the complete current inventory with the
inventory of the last successfully published export.

An operator may instead choose an earlier published export as the base (an
"incremental since" export), as long as its metadata copy is kept. The export
format is unchanged; only base_export_id points further back. Exports published
between that base and the new export are superseded: a receiver that applied the
new export skips them (see docs/importer.md). The next regular incremental export
builds on the new export.

The inventory includes relative path, type, size, nanosecond mtime and SHA-256.
Symlink entries also contain their relative target.

Only new or changed entries enter the archive. Removed paths enter deleted.txt.

A full export has no base identifier and includes the complete current inventory
as its payload.

Incremental chains must be applied in base order by the receiving implementation.

## Repair exports

A repair export answers a receiver's repair request (`repair-<slug>.json`, written by
the importer's `check` when the receiver's tree differs from the last applied
export; format in docs/importer.md). The operator uploads the request and its
`.sha256` file on the Exports page. The sender checks:

- the `.sha256` line names `repair-<slug>.json` of this repository and matches,
- format `mirrorhub-repair-request`, version 1, the repository's slug,
- `base_export_id` is a published export with a kept metadata copy,
- every `resend` path is in that export's files.json and no `delete` path is,
- all paths are safe relative paths; at most 16 MiB.

It then shows what the export will do and queues it on confirmation. The request
is kept under `<state>/manifests/repair-requests/<slug>/<sha256>.json` until the
export is published (unconfirmed uploads are removed after a week).

The export is an incremental export since `base_export_id` ("incremental since",
above) with two additions:

- every `resend` path that still exists on the sender enters the payload, even if
  it did not change since the base;
- every `delete` path that does not exist on the sender enters deleted.txt, unless
  the operator chose to keep the receiver's extra files.

manifest.json carries the extra field `repair`:

```json
"repair": {
  "request_sha256": "…",
  "requested_at": "2026-11-26T09:00:00+00:00",
  "check": "quick",
  "resend": 2,
  "delete": 1,
  "keep_extra": false,
  "skipped": 0
}
```

`resend` and `delete` count the paths the request added. Everything else is the
ordinary incremental format; `mirrorhub.verify` ignores unknown fields, and an
importer without repair support applies it as an ordinary incremental export. An
importer with repair support compares the whole tree with files.json afterwards.
Exports published between the base and the repair export are superseded.

## Snapshot consistency

The worker serializes sync and export execution for each repository.

A successful non-dry sync creates an inventory immediately after the tool exits.

For chained exports, the export job requires the current inventory to match that
sync inventory. Another sync in the queueing gap can therefore make a chained
export fail. MirrorHub refuses publication rather than guessing which files belong
to the earlier sync.

An export checks the repository again after packing. Changes during packing
cause publication to be refused.

Operators and other applications must not modify repository data while a worker
is using it. The advisory repository guard only coordinates MirrorHub workers.

Inventory collection hashes file contents with SHA-256. To avoid rehashing a
whole mirror after every sync, the sync worker and incremental exports keep a
per-repository hash cache on the local state volume
(`manifests/hashcache-<slug>.json`). A cached hash is reused only when the
file's inode, size, mtime and ctime are all unchanged. ctime cannot be set from
user space, so rewriting a file and restoring its old size and mtime still
forces a rehash. Files changed within 60 seconds before the inventory started
are hashed but not cached, which covers coarse kernel timestamps and moderate
NFS clock skew.

The cache does not change the inventory format or the export contract. A
missing or damaged cache only costs time: everything is rehashed.

Exports read each payload file once. Their inventory hashes nothing: files
without a cache hit are hashed while they are copied into the archive, and the
digests go into `files.json` and the cache. An incremental export hashes before
the diff only the files without a cache hit whose size and mtime still equal the
base entry (for example a new inode with the old content), because only their
content decides whether they belong in the payload.

Full exports ignore the cache, rehash every file and rebuild the cache from the
results. A *Rehash* job (manual or scheduled) rebuilds the cache without an
export and reports files whose content changed while inode, size, mtime and
ctime stayed the same; it keeps their old cache entries, so no export takes
that content silently (README "Rehash"). Run a rehash if the hash cache is ever
in doubt, for example after storage-level repairs on the mirror volume, and
periodically (monthly) to catch silent disk corruption.

## Verification

On a receiving machine with Python 3.12:

    python -m mirrorhub.verify /path/to/export-directory

The verifier uses the Python standard library only. A copy of the mirrorhub
package containing __init__.py and verify.py is sufficient; FastAPI and database
dependencies are not needed for this command.

Verification checks:

- Completion marker and export identity.
- Required files and exact checksum coverage.
- SHA-256 hashes of archive parts and metadata files.
- Inventory entry shape and relative paths.
- Payload and deletion counts.
- Full/incremental base semantics.

Extra manifest fields (such as `repair`) are ignored.

It does not extract the archive, validate an incremental chain against a local
receiver state, or verify a detached GPG signature.

A successful checksum check detects accidental corruption. It does not prove
authenticity if an attacker can replace both files and checksums.

## Optional signing

The public-key import page is not a private signing-key provisioning interface.

An operator must provision a dedicated signing key into the existing GPG volume
using a controlled administrative procedure.

Set MIRRORHUB_SIGNING_KEY in .env to its full primary-key fingerprint and recreate
worker-export so it receives the new environment.

The existing Compose worker-export service loads .env through env_file.

The key must support unattended signing. Encrypted private keys that require an
interactive passphrase are not supported by this implementation.

Use a dedicated, restricted signing key. Do not place a personal master key in
the volume.

The export worker copies the GPG home into a private temporary home for signing,
because its mounted GPG volume is read-only. Private key material must therefore
be protected both on the local GPG volume and on the local state volume.

The receiver must validate MANIFEST.sha256.sig using an independently provisioned
trusted public key. Never establish trust using a key delivered only in the same
untrusted export.

Example detached-signature verification, outside the stdlib verifier:

    gpgv --keyring /trusted/export-signers.gpg \
      MANIFEST.sha256.sig MANIFEST.sha256

## Failure and recovery

A failure before .done leaves no consumable export.

Temporary directories are retained.

If a worker dies after creating .done but before recording publication in SQLite,
a retry verifies and recovers that job's already published export and writes the
`.published` marker of its metadata copy if it is missing.

If the database is lost, the export history is restored from the marked metadata
copies (scheduler, before each incremental export, or
`python -m mirrorhub.export.reconcile`); incremental exports continue from the
newest marked copy. Only the loss of the state volume's export-meta/ forces a full
export.

A final directory without .done remains incomplete and must not be picked up.

A timestamp collision is reported as an error; existing directories are never
overwritten.

## Cancellation

Cancellation is cooperative during hashing, packing and splitting.

External compression and signing can delay cancellation until their current
operation returns.

Once publication begins, MirrorHub completes publication instead of interrupting
the rename/marker sequence.

## Notifications

Export jobs use the normal job_succeeded, job_failed and retry_exhausted rules.
A published export additionally raises export_published with the export id, mode,
base export id, file and deletion counts and the archive size.

Create a channel and event rules before relying on notifications.

Each finished job is notified exactly once, independent of the order in which
jobs finish. Deliveries are queued and retried with backoff; a delivery that
still fails after the last retry is recorded in the audit log.

A file channel writes events as JSON files to `<drop folder>/_events/`. The
pickup tool may transfer them with the exports; they are not part of any export
and the importer ignores directories starting with `_`.

The export .done contract, job history and audit log remain the completion
records.