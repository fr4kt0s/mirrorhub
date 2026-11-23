# MirrorHub – P1 acceptance checklist

As of 23 Nov 2026 (PR #50). Extract from the [operations handbook](operations-handbook.md), section 3.5: only the 45 tests with priority P1, i.e. everything that must hold before production use.

- The IDs (e.g. **G26**) are the same as in the handbook. "see 3.x" refers to the practical tests with commands in the handbook, which also lists the P2/P3 tests.
- **[test system]**: changes or destroys data. Run only on a test system or with a throwaway repository.
- `MH` = `python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml`
- If a result differs, note the date, commit (`--version` or footer) and output.

Recommended order: A → F (sender), then G (receiver), then H (diode), finally I and J.

Accepted by: ______________________  Date: ____________  Commit: ____________

## A. Sender: deployment, login, security

- [ ] **A1** `docker compose ps` → all services `healthy`, `init-volumes` `exited (0)`.
- [ ] **A2** Database revision (1.1 step 5) → `0009`.
- [ ] **A3** Smoke test → 8/8.
- [ ] **A4** Commit in the footer and in `web`, `worker` and `worker-export` equals `git rev-parse --short=12 HEAD`:
  `for s in web worker worker-export; do docker compose exec $s python -c "from mirrorhub import buildinfo; print('$s', buildinfo.describe())"; done`
- [ ] **A5** CA certificate (`make root-cert`) installed on a client → browser opens the interface without a certificate warning.
- [ ] **A6** Log in with the admin account → dashboard; wrong password → error message, no login.
- [ ] **A7** Open the interface via another name or the IP instead of `MIRRORHUB_HOSTNAME` → login rejected with 403 (confirms `MIRRORHUB_PUBLIC_ORIGIN` is in effect).

## B. Keys

- [ ] **B1** Upload a key file (`.asc`/`.gpg`) → appears with fingerprint and user ID.
- [ ] **B2** Import keys from a keyring package (import package) → the contained keys appear.
- [ ] **B3** Sync a signed debmirror repository with the matching key → succeeds.

## C. Repositories and sync

- [ ] **C1** Create and sync a debmirror repository → job `succeeded`, `/data/<slug>/` contains `dists/` and `pool/`.
- [ ] **C2** ftpsync with empty *Architectures to include* → sync starts, no configuration error (#41).
- [ ] **C3** ftpsync with include only (e.g. `amd64`) → only that architecture under `dists/*/binary-*` and in `pool`.
- [ ] **C7** Log of an ftpsync job → no debmirror entries (original finding).
- [ ] **C10** Configuration error (e.g. invalid option) → job `failed` at once, no retry, the message names the cause and does not contain "exit code None".
- [ ] **C20** Create a schedule → sync starts at the scheduled time.

## D. Jobs and logs

- [ ] **D1** Delete the newest job, start a new one → new, higher ID; an old ID is never reused (#39).
- [ ] **D2** Log starts with a `# run job <id>` header; a retry adds another header and keeps the old log.

## E. Exports (sender)

- [ ] **E1** First export of a repository requested as `incr` → created as `full`.
- [ ] **E2** `incr` after changes to the mirror → contains only changed and new files; `deleted.txt` lists removed ones.
- [ ] **E3** Export directory contains `MANIFEST.sha256`, `files.json`, `manifest.json`, `deleted.txt` and, last, `.done`.
- [ ] **E4** `docker compose exec worker-export python -m mirrorhub.verify /export/<slug>/<export-id>` → ok.
- [ ] **E9** `manifest.json` → `tool_versions.mirrorhub_commit` equals the sender commit.
- [ ] **E15** Markers: every published metadata copy has `.published` (commands in 3.1).
- [ ] **E16** Incremental since → see 3.3.
- [ ] **E21** [test system] Database loss: keep the state volume, delete the database, recreate the repositories via YAML, run `reconcile` → export history is back; the next `incr` builds on the last export, the importer applies it without `--rebase`.

## F. Notifications

- [ ] **F1** Create a webhook channel, *Test* → receiver gets the message.
- [ ] **F2** SMTP channel, *Test* → mail arrives.
- [ ] **F3** File channel, *Test* → JSON file in `/export/_events/`.

## G. Receiver: importer

- [ ] **G1** `sha256sum -c mirrorhub-importer.pyz.sha256` → OK.
- [ ] **G2** `$MH --version` → commit equals the sender's footer.
- [ ] **G4** First full into an empty tree → exit 0, `status` shows `last`.
- [ ] **G5** Full into a non-empty tree without `--rebase` → exit 5, tree unchanged.
- [ ] **G6** Incremental without an applied predecessor (chain gap) → exit 5, tree unchanged.
- [ ] **G7** Apply the same export again → nothing to do, exit 0.
- [ ] **G9** [test system] Tamper with a copy of an export, apply it → exit 3, tree unchanged.
- [ ] **G21** Late export after "Incremental since" → see 3.3 (superseded, exit 0).
- [ ] **G25** Detection practical test → see 3.2.
- [ ] **G26** [test system] Change a file in the tree, then import → export is applied, exit 11; with `--drift-output DIR`, the report files are in `DIR/<slug>/`.
- [ ] **G40** Client with `sources.list` pointing at the receiver mirror → `apt update` without errors, a package can be installed.

## H. End to end through the real diode

- [ ] **H1** Sync → export → pickup → diode → inbox → import without manual intervention.
- [ ] **H2** Pickup transfers only exports with `.done`, never half-finished ones.

## I. Backup and restore

- [ ] **I1** Backup command (2.1) → file exists; `sqlite3 <file> "pragma integrity_check"` → `ok`.
- [ ] **I2** The backup and `export-meta` are stored outside the Docker host.

## J. Operations and monitoring

- [ ] **J1** `systemctl list-timers mirrorhub-check.timer` → timer active, next run set.

---

**Result:** [ ] all P1 tests passed  [ ] deviations (see notes)

Notes:
