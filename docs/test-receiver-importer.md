# Receiver test: importer performance, logs, events, Checkmk, web UI

Test plan for the receiver for #57 (faster first imports), #58 (readable log lines),
#40 (event files and Checkmk), #62 (web UI) and #63 (permission check). All five are
merged; one importer file built from `master` contains them.

Result to report back: the timing table (section 2.6), exit codes, and anything that
differs from "expected".

Tester: ______________________  Date: ____________  Commit (`--version`): ____________

## 0. Preparation

### 0.1 Build and carry the importer

On the sender host:

```sh
git switch master && git pull --ff-only
make importer                                  # dist/mirrorhub-importer.pyz + .sha256
```

Carry `dist/mirrorhub-importer.pyz` and `dist/mirrorhub-importer.pyz.sha256` to the
receiver, for example into `/tmp/new/`. **Keep the importer you use today** for the
comparison; below it is called `mirrorhub-importer-old.pyz`.

On the receiver (check the checksum before renaming; the `.sha256` file names the
original file):

```sh
cd /tmp/new && sha256sum -c mirrorhub-importer.pyz.sha256      # OK
sudo cp /opt/mirrorhub/mirrorhub-importer.pyz /opt/mirrorhub/mirrorhub-importer-old.pyz
sudo install -m 0644 /tmp/new/mirrorhub-importer.pyz /opt/mirrorhub/mirrorhub-importer-new.pyz
python3 /opt/mirrorhub/mirrorhub-importer-old.pyz --version    # today's commit
python3 /opt/mirrorhub/mirrorhub-importer-new.pyz --version    # commit of master (git rev-parse --short HEAD on the sender)
```

- [ ] **0.1** New importer reports the `master` commit; old importer still available.

### 0.2 Short names for the commands

```sh
IU=mirrorhub                      # the user that runs the import timer (root, if it runs as root today)
OLD="sudo -u $IU python3 /opt/mirrorhub/mirrorhub-importer-old.pyz"
NEW="sudo -u $IU python3 /opt/mirrorhub/mirrorhub-importer-new.pyz"
SLUG=download.docker.com          # the repository to measure; best the largest one
```

Run every importer command as that one user (timer, `check`, web UI, by hand). A run
as another user, e.g. with plain `sudo`, leaves directories the next run cannot
change; since #63 the importer then stops before changing anything (exit code 9, see
section 7 and the operations handbook, section 3.6).

## 1. Test configuration (does not touch the real mirror)

The timing test imports into **empty scratch directories** with a separate
`importer.toml`. It reads the real inbox (read only) and never writes to the real
mirror tree or the real state.

Put the scratch directories on **the same file system as the real mirror** (here
the NFS share), otherwise the timing says nothing about the real case. They need
free space for one copy of the measured repository, for both runs one after the
other.

```sh
du -sh /mnt/import/mirrorhub/data/$SLUG        # size of the repository
find /mnt/import/mirrorhub/data/$SLUG | wc -l  # number of files and directories
df -h /mnt/import                              # free space
ls /mnt/repository/export/$SLUG/               # a full export must be there (…-full)
```

`/opt/mirrorhub/importer-test.toml`:

```toml
export_dir  = "/mnt/repository/export"          # the real inbox, only read
mirror_root = "/mnt/import/mirrorhub-test/data"  # scratch, same NFS share
state_dir   = "/mnt/import/mirrorhub-test/state" # scratch
```

```sh
TEST="--config /opt/mirrorhub/importer-test.toml"
```

- [ ] **1.1** A full export of `$SLUG` is in the inbox; enough free space.

If the inbox holds no full export of `$SLUG` any more, export one on the sender
(Exports page, mode *full*) and let the pickup tool carry it.

## 2. Runtime: old vs. new importer with `time`

### 2.1 How `time` works

`time` in front of a command measures it and prints three values when it ends:

```
real    41m12.507s     ← wall clock: how long you waited. This is the number that counts.
user    6m03.118s      ← CPU time in the importer itself (hashing, unpacking, Python)
sys     9m47.902s      ← CPU time in the kernel (file system calls; high with many files and NFS)
```

`real` much larger than `user + sys` means the importer mostly waited for the disk
or the network (NFS, `fsync`). That waiting is what #57 removes.

`time` is built into bash. More detail (peak memory, file system operations,
context switches) comes from GNU time, if installed (`apt install time`):
`/usr/bin/time -v <command>`.

The output of `time` goes to the terminal at the end. To keep it together with the
importer's output in a file:

```sh
{ time <command> ; } 2>&1 | tee result.txt
```

### 2.2 Same starting conditions for both runs

Before **each** of the two runs:

```sh
sudo systemctl stop mirrorhub-import.timer mirrorhub-check.timer   # no timer run in between (names as on your host)
sudo rm -rf /mnt/import/mirrorhub-test           # empty scratch tree and state
sudo -u $IU mkdir -p /mnt/import/mirrorhub-test
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches   # empty the page cache, so no run reads from memory
```

Dropping the cache matters: without it the second run reads the inbox partly from
memory and looks faster than it is.

### 2.3 Run 1: old importer

```sh
{ time $OLD $TEST import --slug $SLUG ; } 2>&1 | tee /tmp/import-old.txt
echo "exit code: ${PIPESTATUS[0]}"
```

The old importer prints JSON lines and gives no progress; a long silence is normal.

### 2.4 Run 2: new importer

Prepare again (section 2.2), then:

```sh
{ time $NEW $TEST import --slug $SLUG ; } 2>&1 | tee /tmp/import-new.txt
echo "exit code: ${PIPESTATUS[0]}"
```

The new importer prints a progress line every 30 seconds (phase, percent, files,
MiB/s, time left) and `phase done` with the duration of each phase:

```sh
grep -E "phase done|progress" /tmp/import-new.txt | tail -20
```

### 2.5 Both trees must be identical in content

After run 2 the scratch tree holds the new importer's result. Check it fully:

```sh
$NEW $TEST check --slug $SLUG --hash ; echo "exit code: $?"     # expected 0
```

- [ ] **2.5** `check --hash` exit 0 ("… entries match …").

### 2.6 Record the result

| | old importer | new importer |
|---|---|---|
| `real` | | |
| `user` | | |
| `sys` | | |
| exit code | | |
| files / size of `$SLUG` (section 1) | | |

Optional, for a second data point: repeat with a repository with many small files,
or let the benchmark script measure synthetic data on the same NFS share (section 8).

### 2.7 Clean up

```sh
sudo rm -rf /mnt/import/mirrorhub-test
sudo systemctl start mirrorhub-import.timer mirrorhub-check.timer
```

- [ ] **2.7** Scratch directories removed, timer running again.

## 3. Real configuration with the new importer

Add to the real `/etc/mirrorhub/importer.toml` (directories are created on the
first run; the importer's user needs write access):

```toml
events_dir  = "/var/lib/mirrorhub-importer/events"
reports_dir = "/var/lib/mirrorhub-importer/reports"
```

Then use the new importer for real:

```sh
sudo install -m 0644 /opt/mirrorhub/mirrorhub-importer-new.pyz /opt/mirrorhub/mirrorhub-importer.pyz
MH="sudo -u $IU python3 /opt/mirrorhub/mirrorhub-importer.pyz --config /etc/mirrorhub/importer.toml"
$MH import --all ; echo "exit code: $?"
ls /var/lib/mirrorhub-importer/events/ | tail
$MH check --all ; echo "exit code: $?"
ls /var/lib/mirrorhub-importer/reports/*/summary.txt
```

To go back: `cp mirrorhub-importer-old.pyz mirrorhub-importer.pyz` (the two new
keys are then refused by the old importer as unknown: comment them out).

- [ ] **3.1** `import --all` exit 0 (or 11 if the tree already differed; then see 3.2).
- [ ] **3.2** `check --all` exit 0; with 11, `reports/<slug>/*.txt` list the differences.
  No `warning: … cannot change N directories` (otherwise `sudo chown -R $IU: <mirror_root>/<slug>`, see section 7).
- [ ] **3.3** Event files appear (`import_run`, `import_applied`, `tree_check`).

## 4. Log output (#58)

```sh
$MH status --log-level debug                    # in a terminal: readable text lines, colours
$MH status --log-level debug 2>&1 | cat         # through a pipe: JSON lines as before
$MH status --log-level debug --log-format json  # JSON forced
```

- [ ] **4.1** Terminal: one readable line per event (time, level, event, repository, export).
- [ ] **4.2** Pipe / journal: JSON lines, keys start with `ts`, `level`, `event`.

## 5. Web UI (#62)

Steps in [importer-ui.md](importer-ui.md), section "Setup step by step". Short test:

```sh
$MH serve                       # http://127.0.0.1:8080/ ; from a workstation:
ssh -L 8080:127.0.0.1:8080 receiver   # then open http://localhost:8080/
```

- [ ] **5.1** Overview lists all repositories, last import, tree status, pending exports.
- [ ] **5.2** *Dry run* on a repository: run page with live output, exit 0, nothing changed.
- [ ] **5.3** *Check* on a repository: result in the overview, report page reachable.
- [ ] **5.4** *History* shows the runs of section 3 (trigger `cli`) and of the UI (`ui`).
- [ ] **5.5** A pending export opens its page (mode, base, fits the receiver, size).
- [ ] **5.6** Stop the UI (Ctrl+C), start again: the runs are still listed.
- [ ] **5.7** Optional: on the phone (through the tunnel) the page fits the screen.

## 6. Checkmk (#40, optional)

Steps in [checkmk.md](checkmk.md). Short test after installing the local check
with `importer_events_dir = /var/lib/mirrorhub-importer/events`:

```sh
/usr/lib/check_mk_agent/local/mirrorhub_events
```

- [ ] **6.1** Lines `MirrorHub import <repo>`, `MirrorHub tree <repo>`, `MirrorHub importer` appear, state 0 after a clean import and check.
- [ ] **6.2** Service discovery in Checkmk finds the services.

## 7. User and permissions (#63)

Uses the scratch tree of section 2 (after run 2 it holds `$SLUG` and a state). Give
one directory to another user, check, give it back:

```sh
D=$(cd /mnt/import/mirrorhub-test/data/$SLUG && ls -d */ | head -1)   # e.g. dists/
sudo chown nobody /mnt/import/mirrorhub-test/data/$SLUG/$D
$NEW $TEST check --slug $SLUG ; echo "exit code: $?"
sudo chown $IU: /mnt/import/mirrorhub-test/data/$SLUG/$D
$NEW $TEST check --slug $SLUG ; echo "exit code: $?"
```

On NFS with `root_squash`, `chown` as root may be refused on the client; then run it
on the NFS server or skip 7.1.

- [ ] **7.1** First `check`: exit 0 and `warning: user … cannot change 1 directory`, naming the directory with owner and mode and a `fix: sudo chown -R …` line.
- [ ] **7.2** Second `check`: no warning.
- [ ] **7.3** The real tree (section 3.2) shows no warning. If an import ever stops with `permission check failed` (exit 9), the tree is unchanged: run `chown -R` and the same import again.

## 8. Optional: benchmark on the receiver's disk

`scripts/bench_importer.py` (in the checkout on the sender, or copy it with the
`src/` directory) compares two importer versions on synthetic exports. With
`--work` on the NFS share it shows the effect of the share without real data:

```sh
git worktree add /tmp/importer-before 7a3f3b2
python3 scripts/bench_importer.py --work /mnt/import/mirrorhub-bench \
  --src before=/tmp/importer-before/src --src after=src
rm -rf /mnt/import/mirrorhub-bench
```

---

**Result:** [ ] all checks passed  [ ] deviations (see notes)

Notes:
