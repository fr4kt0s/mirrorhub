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
- manifest.json, describing the export identity, base and payload.
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
3. Read exports without modifying their files.
4. Preserve all files, including .done.
5. Transfer full exports before dependent incremental exports.
6. Preserve export identifiers and base identifiers.
7. Deduplicate by repository slug and export identifier.

The pickup tool MUST NOT remove the sender's published export directories.
MirrorHub reads the last successful inventory there to construct the next diff.

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

The inventory includes relative path, type, size, nanosecond mtime and SHA-256.
Symlink entries also contain their relative target.

Only new or changed entries enter the archive. Removed paths enter deleted.txt.

A full export has no base identifier and includes the complete current inventory
as its payload.

Incremental chains must be applied in base order by the receiving implementation.

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

Inventory collection performs full hashing and can take considerable time.
This implementation favors correctness over hashing shortcuts.

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
a retry verifies and recovers that job's already published export.

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

Create a channel and event rules before relying on notifications.

The existing Phase 5 notification sweep still uses a job-ID high-water mark.
It can miss an older job that finishes after a newer one. Do not treat those
notifications as reliable completion evidence until that sweep is replaced in
the final review.

The export .done contract, job history and audit log are the completion records.