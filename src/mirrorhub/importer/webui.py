"""Small web UI for the importer: `python3 mirrorhub-importer.pyz --config importer.toml serve`.

Standard library only, so it ships inside mirrorhub-importer.pyz. Every action runs
the importer CLI itself as a child process (import, check, adopt, status with the
same options), so locks, exit codes, event files and log output are exactly those of
the command line; the page shows the output live.

No user accounts. By default the server listens on 127.0.0.1 only. Optional
password (no user name): environment variable MIRRORHUB_IMPORTER_UI_PASSWORD.
Because a click can start an import, every form carries a CSRF token, cross-origin
posts are refused, and requests must name an allowed host (localhost, an IP address,
or a name in MIRRORHUB_IMPORTER_UI_HOSTS) against DNS rebinding.

One run at a time; the last runs and their output are kept in memory until the
server stops. Cancelling sends SIGTERM; an interrupted import continues from its
journal on the next run (docs/importer.md).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import mirrorhub
from mirrorhub import buildinfo
from mirrorhub.importer.checks import EXPORT_ID_RE, SLUG_RE
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import ImporterError
from mirrorhub.importer.state import StateStore

PASSWORD_ENV = "MIRRORHUB_IMPORTER_UI_PASSWORD"  # noqa: S105 - variable name, not a secret
HOSTS_ENV = "MIRRORHUB_IMPORTER_UI_HOSTS"
KEEP_RUNS = 50
MAX_OUTPUT = 4 * 1024 * 1024  # characters kept per run (the oldest are dropped)
MAX_BODY = 64 * 1024
COOKIE = "mh_importer_ui"

EXIT_CODES = {
    0: "ok",
    1: "unexpected error",
    2: "configuration error",
    3: "verification failed",
    4: "signature check failed",
    5: "chain error",
    6: "unsafe archive",
    7: "another run holds the lock",
    8: "tool missing",
    9: "I/O error",
    10: "post-check failed",
    11: "tree differs from files.json",
    -15: "cancelled",
}

# Runs the importer CLI from the same code as this server (source tree or .pyz).
BOOT = "import sys; from mirrorhub.importer.cli import main; sys.exit(main(sys.argv[1:]))"


class FormError(ValueError):
    """Invalid form input; shown to the user."""


@dataclass
class Run:
    id: int
    title: str
    argv: list[str]
    started: float = field(default_factory=time.time)
    finished: float | None = None
    exit_code: int | None = None
    output: str = ""
    process: subprocess.Popen[str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def append(self, text: str) -> None:
        with self.lock:
            self.output += text
            if len(self.output) > MAX_OUTPUT:
                self.output = "[… older output dropped …]\n" + self.output[-MAX_OUTPUT:]

    def state(self) -> str:
        if self.running:
            return "running"
        if self.exit_code == 11:
            return "differs"  # finished, but the tree differs from files.json
        return "ok" if self.exit_code == 0 else "failed"


class Runner:
    """Starts importer commands as child processes, one at a time."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.runs: OrderedDict[int, Run] = OrderedDict()
        self._next = 1
        self._lock = threading.Lock()
        root = Path(mirrorhub.__file__).resolve().parents[1]  # src dir or the .pyz
        self._env = dict(os.environ)
        self._env["PYTHONPATH"] = os.pathsep.join(
            [str(root), *filter(None, [os.environ.get("PYTHONPATH")])]
        )
        self._env["PYTHONUNBUFFERED"] = "1"

    def active(self) -> Run | None:
        return next((run for run in self.runs.values() if run.running), None)

    def start(self, title: str, args: list[str]) -> Run:
        with self._lock:
            if self.active() is not None:
                raise FormError("another run is still active; wait for it or cancel it")
            argv = [
                "--config",
                str(self.config_path),
                "--log-format",
                "text",
                "--progress-interval",
                "10",
                *args,
            ]
            run = Run(self._next, title, argv)
            self._next += 1
            self.runs[run.id] = run
            while len(self.runs) > KEEP_RUNS:
                self.runs.popitem(last=False)
            run.process = subprocess.Popen(  # noqa: S603 - fixed interpreter, validated args
                [sys.executable, "-c", BOOT, *argv],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                env=self._env,
            )
        threading.Thread(target=self._collect, args=(run,), daemon=True).start()
        return run

    def _collect(self, run: Run) -> None:
        process = run.process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            run.append(line)
        run.exit_code = process.wait()
        run.finished = time.time()
        meaning = EXIT_CODES.get(run.exit_code, "")
        run.append(f"\n# exit code {run.exit_code}" + (f" ({meaning})" if meaning else "") + "\n")

    def cancel(self, run: Run) -> None:
        if run.running and run.process is not None:
            run.append("\n# cancel requested (SIGTERM)\n")
            run.process.terminate()


# ---- Building command lines from form fields ----------------------------------------


def _one(form: dict[str, list[str]], name: str) -> str:
    return (form.get(name) or [""])[0].strip()


def _flag(form: dict[str, list[str]], name: str) -> bool:
    return _one(form, name) == "on"


def _slug(value: str) -> str:
    if not SLUG_RE.fullmatch(value):
        raise FormError(f"invalid repository slug: {value!r}")
    return value


def _absolute(value: str, label: str) -> str:
    if not value.startswith("/") or "\x00" in value:
        raise FormError(f"{label} must be an absolute path")
    return value


def build_command(form: dict[str, list[str]], config: Config) -> tuple[str, list[str]]:
    """Title and CLI arguments for a submitted form; raises FormError."""
    command = _one(form, "command")
    if command == "status":
        slug = _one(form, "slug")
        return (
            "status " + (slug or "(all)"),
            ["status", *(["--slug", _slug(slug)] if slug else [])],
        )

    if command == "import":
        scope = _one(form, "scope")
        args = ["import"]
        if scope == "all":
            args.append("--all")
            title = "import --all"
        elif scope == "slug":
            slug = _slug(_one(form, "slug"))
            args += ["--slug", slug]
            title = f"import {slug}"
        elif scope == "export":
            path = Path(_absolute(_one(form, "export"), "export directory"))
            inbox = config.export_dir.resolve()
            if not path.resolve().is_relative_to(inbox) or not path.is_dir():
                raise FormError(f"export directory must be an existing directory below {inbox}")
            args += ["--export", str(path)]
            title = f"import {path.parent.name}/{path.name}"
        else:
            raise FormError("choose what to import")
        for name in ("dry_run", "full_check", "no_tree_check", "verify_first"):
            if _flag(form, name):
                args.append("--" + name.replace("_", "-"))
        dangerous = [name for name in ("rebase", "discard_staging") if _flag(form, name)]
        if dangerous:
            if scope == "all":
                raise FormError(
                    "--rebase and --discard-staging cannot be used with all repositories"
                )
            if not _flag(form, "confirm"):
                raise FormError("tick the confirmation to use --rebase or --discard-staging")
            args += ["--" + name.replace("_", "-") for name in dangerous]
        drift = _one(form, "drift_output")
        if drift:
            args += ["--drift-output", _absolute(drift, "drift output directory")]
        if _flag(form, "dry_run"):
            title += " (dry run)"
        return title, args

    if command == "check":
        slug = _one(form, "slug")
        args = ["check", *(["--slug", _slug(slug)] if slug else ["--all"])]
        title = "check " + (slug or "--all")
        if _flag(form, "hash"):
            args.append("--hash")
            title += " --hash"
        output = _one(form, "output_dir")
        if output:
            args += ["--output-dir", _absolute(output, "output directory")]
        return title, args

    if command == "adopt":
        slug = _slug(_one(form, "slug"))
        export_id = _one(form, "export_id")
        args = ["adopt", "--slug", slug]
        if export_id:
            if not EXPORT_ID_RE.fullmatch(export_id):
                raise FormError(f"invalid export id: {export_id!r}")
            args += ["--export-id", export_id]
        else:
            args.append("--auto")
        if _flag(form, "hash"):
            args.append("--hash")
        return f"adopt {slug}", args

    raise FormError("unknown command")


# ---- Overview (read-only, in this process) ------------------------------------------


@dataclass
class RepoRow:
    slug: str
    last_export_id: str | None
    applied_at: str
    applied: int
    superseded: int
    pending: list[str]
    error: str = ""


def overview(config: Config) -> list[RepoRow]:
    slugs: set[str] = set()
    if config.export_dir.is_dir():
        slugs |= {
            child.name
            for child in config.export_dir.iterdir()
            if child.is_dir() and not child.name.startswith("_") and SLUG_RE.fullmatch(child.name)
        }
    if config.state_dir.is_dir():
        slugs |= {p.stem for p in config.state_dir.glob("*.json") if SLUG_RE.fullmatch(p.stem)}
    rows: list[RepoRow] = []
    for slug in sorted(slugs):
        try:
            state = StateStore(config.state_dir, slug).load()
        except ImporterError as exc:
            rows.append(RepoRow(slug, None, "", 0, 0, [], str(exc)))
            continue
        superseded = sum(1 for item in state.history if item.get("status") == "superseded")
        applied_at = ""
        for item in reversed(state.history):
            if item.get("export_id") == state.last_export_id and item.get("status") != "superseded":
                applied_at = str(item.get("applied_at") or item.get("adopted_at") or "")
                break
        pending: list[str] = []
        inbox = config.export_dir / slug
        if inbox.is_dir():
            pending = [
                child.name
                for child in sorted(inbox.iterdir())
                if EXPORT_ID_RE.fullmatch(child.name)
                and (child / ".done").is_file()
                and not state.applied(child.name)
                and not state.superseded(child.name)
            ]
        rows.append(
            RepoRow(
                slug,
                state.last_export_id,
                applied_at,
                len(state.history) - superseded,
                superseded,
                pending,
            )
        )
    return rows


# ---- HTML ------------------------------------------------------------------------------

STYLE = """
:root { color-scheme: light dark; --bg: #f6f7f8; --surface: #fff; --ink: #1d2327;
  --muted: #5f6b73; --line: #dde2e5; --accent: #127a7a; --bad: #b42318; --warn: #b54708; }
@media (prefers-color-scheme: dark) { :root { --bg: #0e1416; --surface: #141c1f;
  --ink: #e3e8ea; --muted: #8d9aa1; --line: #26343a; --accent: #4fc4c4; --bad: #f97066;
  --warn: #fdb022; } }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 system-ui, sans-serif; background: var(--bg); color: var(--ink); }
header { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
  padding: 0.8rem 1.25rem; border-bottom: 1px solid var(--line); background: var(--surface); }
header a { color: inherit; text-decoration: none; font-weight: 700; }
header .muted { font-size: 0.85rem; }
main { max-width: 1400px; margin: 0 auto; padding: 1.25rem; }
h1 { font-size: 1.4rem; margin: 0 0 1rem; } h2 { font-size: 1.1rem; margin: 0 0 0.75rem; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
  padding: 1rem 1.15rem; margin-bottom: 1.1rem; min-width: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1.1rem; }
.grid .card { margin: 0; }
.muted { color: var(--muted); }
a { color: var(--accent); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-size: 0.85rem; font-weight: 600; }
.scroll { overflow-x: auto; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.86em; }
pre.out { background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 0.75rem; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 70vh;
  overflow-y: auto; margin: 0; }
label { display: block; margin: 0.55rem 0 0.2rem; font-weight: 600; font-size: 0.9rem; }
label.check { display: flex; gap: 0.5rem; align-items: center; font-weight: 400; margin: 0.3rem 0; }
input[type=text], input[type=password], select { width: 100%; padding: 0.42rem 0.55rem;
  border: 1px solid var(--line); border-radius: 6px; font: inherit; color: inherit;
  background: var(--bg); }
button { font: inherit; padding: 0.42rem 0.9rem; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--accent); background: var(--accent); color: var(--surface);
  font-weight: 600; margin-top: 0.75rem; }
button.secondary { background: transparent; color: var(--accent); }
.row-actions { display: flex; gap: 0.35rem; flex-wrap: wrap; }
.row-actions form { margin: 0; } .row-actions button { margin: 0; padding: 0.2rem 0.55rem;
  font-size: 0.85rem; }
.state { font-weight: 600; } .state.ok { color: var(--accent); }
.state.failed { color: var(--bad); }
.state.running, .state.differs { color: var(--warn); }
.alert { border-left: 4px solid var(--bad); padding: 0.6rem 0.8rem; background: var(--surface);
  margin-bottom: 1rem; }
.pending { color: var(--warn); font-weight: 600; }
details { margin-top: 0.5rem; } summary { cursor: pointer; color: var(--muted); }
.grid { margin-bottom: 1.1rem; }
@media (max-width: 640px) {
  main { padding: 0.75rem; }
  table.stack thead { display: none; }
  table.stack, table.stack tbody, table.stack tr { display: block; }
  table.stack tr { padding: 0.5rem 0; border-bottom: 1px solid var(--line); }
  table.stack td { display: flex; gap: 0.75rem; border: 0; padding: 0.15rem 0; }
  table.stack td[data-label]::before { content: attr(data-label); flex: none;
    min-width: 6.5rem; color: var(--muted); font-size: 0.85rem; }
  .runs th:nth-child(3), .runs td:nth-child(3) { display: none; }
}
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _time(stamp: float | None) -> str:
    if stamp is None:
        return ""
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")


def _local(iso: str) -> str:
    """ISO time from the state file (UTC) as local time, or as given if unparsable."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def _duration(run: Run) -> str:
    seconds = round((run.finished or time.time()) - run.started)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Page:
    def __init__(self, ui: WebUI, nonce: str) -> None:
        self.ui = ui
        self.nonce = nonce

    def layout(self, title: str, body: str) -> str:
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{esc(title)} - MirrorHub importer</title><style>{STYLE}</style></head><body>"
            "<header><a href='/'>MirrorHub importer</a>"
            f"<span class='muted'>{esc(buildinfo.describe())} · "
            f"config {esc(self.ui.config_path)}</span>"
            "</header><main>" + body + "</main></body></html>"
        )

    def form(self, command: str, inner: str, button: str, cls: str = "") -> str:
        return (
            f"<form method='post' action='/run'><input type='hidden' name='csrf' "
            f"value='{esc(self.ui.csrf)}'><input type='hidden' name='command' value='{command}'>"
            f"{inner}<button type='submit' class='{cls}'>{esc(button)}</button></form>"
        )

    def quick(self, command: str, fields: dict[str, str], button: str, cls: str = "") -> str:
        hidden = "".join(
            f"<input type='hidden' name='{esc(k)}' value='{esc(v)}'>" for k, v in fields.items()
        )
        return self.form(command, hidden, button, cls)

    def index(self, error: str = "") -> str:
        config = self.ui.config
        try:
            rows = overview(config)
        except OSError as exc:
            rows, error = [], error or f"cannot read inbox or state: {exc}"
        body = "<h1>Repositories</h1>"
        if error:
            body += f"<div class='alert'>{esc(error)}</div>"
        active = self.ui.runner.active()
        if active is not None:
            body += (
                f"<div class='card'>Running: <a href='/runs/{active.id}'>{esc(active.title)}</a> "
                f"since {_time(active.started)}</div>"
            )
        table = (
            "<div class='card scroll'><table class='stack'><thead><tr><th>Repository</th>"
            "<th>Last applied</th>"
            "<th>Applied at</th><th>Pending</th><th>Actions</th></tr></thead><tbody>"
        )
        for row in rows:
            pending = (
                f"<span class='pending'>{len(row.pending)}</span> "
                f"<span class='muted'>{esc(', '.join(row.pending[:3]))}"
                f"{' …' if len(row.pending) > 3 else ''}</span>"
                if row.pending
                else "<span class='muted'>0</span>"
            )
            last = esc(row.last_export_id or "—")
            if row.error:
                last = f"<span class='state failed'>{esc(row.error)}</span>"
            actions = "".join(
                [
                    self.quick("import", {"scope": "slug", "slug": row.slug}, "Import"),
                    self.quick(
                        "import",
                        {"scope": "slug", "slug": row.slug, "dry_run": "on"},
                        "Dry run",
                        "secondary",
                    ),
                    self.quick("check", {"slug": row.slug}, "Check", "secondary"),
                    self.quick(
                        "check", {"slug": row.slug, "hash": "on"}, "Check SHA-256", "secondary"
                    ),
                ]
            )
            table += (
                f"<tr><td><strong>{esc(row.slug)}</strong><br><span class='muted'>"
                f"{row.applied} applied, {row.superseded} superseded</span></td>"
                f"<td data-label='Last applied'><code>{last}</code></td>"
                f"<td data-label='Applied at'>{esc(_local(row.applied_at))}</td>"
                f"<td data-label='Pending'>{pending}</td>"
                f"<td><div class='row-actions'>{actions}</div></td></tr>"
            )
        if not rows:
            table += (
                "<tr><td colspan='5' class='muted'>No repositories in the inbox "
                f"({esc(config.export_dir)}) or the state directory.</td></tr>"
            )
        body += table + "</tbody></table></div>"
        body += "<div class='grid'>" + self.import_card() + self.check_card()
        body += self.adopt_card() + self.status_card() + "</div>"
        body += self.runs_card()
        return self.layout("Repositories", body)

    def import_card(self) -> str:
        inner = (
            "<label>What</label>"
            "<label class='check'><input type='radio' name='scope' value='all' checked> "
            "all repositories (<code>--all</code>)</label>"
            "<label class='check'><input type='radio' name='scope' value='slug'> "
            "one repository</label>"
            "<input type='text' name='slug' placeholder='repository slug, e.g. ubuntu'>"
            "<label class='check'><input type='radio' name='scope' value='export'> "
            "one export directory</label>"
            f"<input type='text' name='export' placeholder='{esc(self.ui.config.export_dir)}"
            "/&lt;slug&gt;/&lt;export-id&gt;'>"
            "<label>Options</label>"
            "<label class='check'><input type='checkbox' name='dry_run'> "
            "dry run (plan only)</label>"
            "<label class='check'><input type='checkbox' name='full_check'> full check "
            "(re-hash the whole tree)</label>"
            "<label class='check'><input type='checkbox' name='verify_first'> verify first "
            "(hash archive parts before extracting)</label>"
            "<label class='check'><input type='checkbox' name='no_tree_check'> "
            "no tree check</label>"
            "<label>Drift output directory (optional)</label>"
            "<input type='text' name='drift_output' "
            "placeholder='/var/lib/mirrorhub-importer/drift'>"
            "<details><summary>Dangerous options</summary>"
            "<label class='check'><input type='checkbox' name='rebase'> <code>--rebase</code>: "
            "full export over existing data (removes paths not in files.json)</label>"
            "<label class='check'><input type='checkbox' name='discard_staging'> "
            "<code>--discard-staging</code>: drop another unfinished import</label>"
            "<label class='check'><input type='checkbox' name='confirm'> I understand</label>"
            "</details>"
        )
        return (
            "<div class='card'><h2>Import</h2>"
            + self.form("import", inner, "Start import")
            + "</div>"
        )

    def check_card(self) -> str:
        inner = (
            "<label>Repository (empty: all)</label><input type='text' name='slug'>"
            "<label class='check'><input type='checkbox' name='hash'> also SHA-256 "
            "(reads every file)</label>"
            "<label>Output directory for the difference lists (optional)</label>"
            "<input type='text' name='output_dir' placeholder='/var/lib/mirrorhub-importer/check'>"
        )
        return (
            "<div class='card'><h2>Check tree</h2><p class='muted'>Compares the tree with "
            "files.json of the last applied export; changes nothing.</p>"
            + self.form("check", inner, "Start check")
            + "</div>"
        )

    def adopt_card(self) -> str:
        inner = (
            "<label>Repository</label><input type='text' name='slug' required>"
            "<label>Export id (empty: try the inbox, newest first)</label>"
            "<input type='text' name='export_id' placeholder='20261124T020000Z-incr'>"
            "<label class='check'><input type='checkbox' name='hash'> also SHA-256</label>"
        )
        return (
            "<div class='card'><h2>Adopt</h2><p class='muted'>Writes a lost state file for a "
            "tree that matches an export exactly; no data is copied.</p>"
            + self.form("adopt", inner, "Start adopt", "secondary")
            + "</div>"
        )

    def status_card(self) -> str:
        inner = "<label>Repository (empty: all)</label><input type='text' name='slug'>"
        return (
            "<div class='card'><h2>Status</h2><p class='muted'>Receiver state as printed by "
            "<code>status</code>.</p>"
            + self.form("status", inner, "Show status", "secondary")
            + "</div>"
        )

    def runs_card(self) -> str:
        runs = list(reversed(self.ui.runner.runs.values()))
        rows = "".join(
            f"<tr><td><a href='/runs/{run.id}'>#{run.id}</a></td><td>{esc(run.title)}</td>"
            f"<td>{_time(run.started)}</td><td>{_duration(run)}</td>"
            f"<td><span class='state {run.state()}'>{run.state()}</span>"
            + (f" <span class='muted'>{run.exit_code}</span>" if run.exit_code is not None else "")
            + "</td></tr>"
            for run in runs
        )
        if not rows:
            rows = "<tr><td colspan='5' class='muted'>No runs since the server started.</td></tr>"
        return (
            "<div class='card scroll'><h2>Runs</h2><table class='runs'><thead><tr>"
            "<th>#</th><th>Command</th>"
            "<th>Started</th><th>Duration</th><th>Result</th></tr></thead><tbody>"
            + rows
            + "</tbody></table></div>"
        )

    def run(self, run: Run) -> str:
        meaning = EXIT_CODES.get(run.exit_code, "") if run.exit_code is not None else ""
        result = f"<span class='state {run.state()}'>{run.state()}</span>" + (
            f" — exit code {run.exit_code} {esc(meaning)}" if run.exit_code is not None else ""
        )
        cancel = ""
        if run.running:
            cancel = (
                f"<form method='post' action='/runs/{run.id}/cancel'><input type='hidden' "
                f"name='csrf' value='{esc(self.ui.csrf)}'>"
                "<button class='secondary'>Cancel</button></form>"
            )
        command = " ".join(["mirrorhub-importer", *run.argv])
        with run.lock:
            output = run.output
        body = (
            f"<h1>Run #{run.id}: {esc(run.title)}</h1><div class='card'>"
            f"<p><code>{esc(command)}</code></p><p>Started {_time(run.started)}, "
            f"<span id='duration'>{_duration(run)}</span> · <span id='result'>{result}</span></p>"
            f"{cancel}</div><div class='card'><pre class='out' id='out'>{esc(output)}</pre></div>"
            "<p><a href='/'>Back to repositories</a></p>"
        )
        if run.running:
            body += f"""<script nonce="{self.nonce}">
(function () {{
  var out = document.getElementById("out"), offset = {len(output)};
  function poll() {{
    fetch("/runs/{run.id}/output?offset=" + offset, {{credentials: "same-origin"}})
      .then(function (r) {{ return r.json(); }})
      .then(function (data) {{
        var stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
        if (data.reset) {{ out.textContent = ""; }}
        out.textContent += data.text; offset = data.offset;
        if (stick) {{ out.scrollTop = out.scrollHeight; }}
        document.getElementById("duration").textContent = data.duration;
        if (data.running) {{ setTimeout(poll, 1500); }} else {{ location.reload(); }}
      }})
      .catch(function () {{ setTimeout(poll, 5000); }});
  }}
  out.scrollTop = out.scrollHeight; setTimeout(poll, 1500);
}})();
</script>"""
        return self.layout(f"Run #{run.id}", body)

    def login(self, error: str = "") -> str:
        message = f"<div class='alert'>{esc(error)}</div>" if error else ""
        return self.layout(
            "Login",
            "<div class='card' style='max-width:360px;margin:10vh auto'><h1>MirrorHub importer</h1>"
            + message
            + "<form method='post' action='/login'><label>Password</label>"
            "<input type='password' name='password' autofocus required>"
            f"<input type='hidden' name='csrf' value='{esc(self.ui.csrf)}'>"
            "<button type='submit'>Log in</button></form></div>",
        )


# ---- Server -----------------------------------------------------------------------------


class WebUI:
    def __init__(
        self,
        config: Config,
        config_path: Path,
        password: str | None = None,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self.config = config
        self.config_path = config_path.resolve()
        self.password = password or None
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.secret = secrets.token_bytes(32)
        self.csrf = hmac.new(self.secret, b"csrf", hashlib.sha256).hexdigest()
        self.session = hmac.new(self.secret, b"session", hashlib.sha256).hexdigest()
        self.runner = Runner(self.config_path)

    def host_allowed(self, host_header: str) -> bool:
        host = urlsplit("//" + host_header).hostname or ""
        if host in ("localhost", *self.allowed_hosts):
            return True
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return False
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "mirrorhub-importer-ui"
    ui: WebUI

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"ui {self.address_string()} {format % args}\n")

    # -- helpers
    def _send(
        self,
        status: int,
        body: str,
        *,
        content_type: str = "text/html",
        nonce: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        script = f"'nonce-{nonce}'" if nonce else "'none'"
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; style-src 'unsafe-inline'; script-src {script}; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _page(self) -> Page:
        return Page(self.ui, secrets.token_urlsafe(16))

    def _logged_in(self) -> bool:
        if self.ui.password is None:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(value, self.ui.session):
                return True
        return False

    def _guard(self) -> bool:
        """Host check for every request; False if a response was sent."""
        if not self.ui.host_allowed(self.headers.get("Host", "")):
            self._send(
                HTTPStatus.FORBIDDEN,
                "Host not allowed. Use localhost or an IP address, or add the name to "
                f"{HOSTS_ENV}.",
                content_type="text/plain",
            )
            return False
        return True

    def _form(self) -> dict[str, list[str]] | None:
        """Parsed POST body after origin and CSRF checks; None if a response was sent."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host", ""):
            self._send(
                HTTPStatus.FORBIDDEN, "cross-origin request refused", content_type="text/plain"
            )
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large", content_type="text/plain"
            )
            return None
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        if not hmac.compare_digest(_one(form, "csrf"), self.ui.csrf):
            self._send(
                HTTPStatus.FORBIDDEN,
                "invalid or missing CSRF token; reload the page",
                content_type="text/plain",
            )
            return None
        return form

    def _run_of(self, part: str) -> Run | None:
        return self.ui.runner.runs.get(int(part)) if part.isdigit() else None

    # -- routes
    def do_GET(self) -> None:
        if not self._guard():
            return
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts == ["health"]:
            self._send(HTTPStatus.OK, "ok", content_type="text/plain")
            return
        if not self._logged_in():
            page = self._page()
            self._send(HTTPStatus.OK, page.login(), nonce=page.nonce)
            return
        if not parts:
            page = self._page()
            self._send(HTTPStatus.OK, page.index(), nonce=page.nonce)
            return
        if len(parts) >= 2 and parts[0] == "runs" and (run := self._run_of(parts[1])):
            if len(parts) == 2:
                page = self._page()
                self._send(HTTPStatus.OK, page.run(run), nonce=page.nonce)
                return
            if parts[2:] == ["output"]:
                query = parse_qs(url.query)
                offset = int((query.get("offset") or ["0"])[0] or 0)
                with run.lock:
                    text = run.output
                reset = offset > len(text)  # output was trimmed
                chunk = text if reset else text[offset:]
                self._send(
                    HTTPStatus.OK,
                    json.dumps(
                        {
                            "text": chunk,
                            "offset": len(text),
                            "reset": reset,
                            "running": run.running,
                            "duration": _duration(run),
                        }
                    ),
                    content_type="application/json",
                )
                return
        self._send(HTTPStatus.NOT_FOUND, "not found", content_type="text/plain")

    def do_POST(self) -> None:
        if not self._guard():
            return
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        form = self._form()
        if form is None:
            return
        if parts == ["login"]:
            given = _one(form, "password")
            if self.ui.password is not None and hmac.compare_digest(
                given.encode(), self.ui.password.encode()
            ):
                cookie = f"{COOKIE}={self.ui.session}; Path=/; HttpOnly; SameSite=Strict"
                self._redirect("/", {"Set-Cookie": cookie})
                return
            time.sleep(1)
            page = self._page()
            self._send(HTTPStatus.UNAUTHORIZED, page.login("wrong password"), nonce=page.nonce)
            return
        if not self._logged_in():
            self._redirect("/")
            return
        if parts == ["run"]:
            try:
                title, args = build_command(form, self.ui.config)
                run = self.ui.runner.start(title, args)
            except FormError as exc:
                page = self._page()
                self._send(HTTPStatus.BAD_REQUEST, page.index(str(exc)), nonce=page.nonce)
                return
            self._redirect(f"/runs/{run.id}")
            return
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "cancel":
            target = self._run_of(parts[1])
            if target is not None:
                self.ui.runner.cancel(target)
                self._redirect(f"/runs/{target.id}")
                return
        self._send(HTTPStatus.NOT_FOUND, "not found", content_type="text/plain")


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ValueError(f"--listen must be HOST:PORT, got {value!r}")
    return host.strip("[]") or "127.0.0.1", int(port)


def make_server(ui: WebUI, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ui": ui})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(config: Config, config_path: Path, listen: str) -> int:
    host, port = parse_listen(listen)
    password = os.environ.get(PASSWORD_ENV) or None
    hosts = tuple(h.strip() for h in os.environ.get(HOSTS_ENV, "").split(",") if h.strip())
    ui = WebUI(config, config_path, password, hosts)
    server = make_server(ui, host, port)
    exposed = host not in ("127.0.0.1", "::1", "localhost")
    print(
        f"MirrorHub importer UI on http://{host if ':' not in host else f'[{host}]'}:{port}/ "
        f"(config {ui.config_path}, {'password' if password else 'no login'})",
        flush=True,
    )
    if exposed and password is None:
        print(
            f"warning: listening on {host} without a password; set {PASSWORD_ENV} "
            "or restrict access (firewall, port binding to 127.0.0.1)",
            file=sys.stderr,
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        active = ui.runner.active()
        if active is not None:
            ui.runner.cancel(active)
        server.server_close()
    return 0
