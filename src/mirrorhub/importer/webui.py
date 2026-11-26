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
import re
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
from mirrorhub.importer import events, repair, webdata
from mirrorhub.importer.checks import EXPORT_ID_RE, SLUG_RE
from mirrorhub.importer.config import Config
from mirrorhub.importer.errors import ImporterError
from mirrorhub.importer.state import StateStore

PASSWORD_ENV = "MIRRORHUB_IMPORTER_UI_PASSWORD"  # noqa: S105 - variable name, not a secret
HOSTS_ENV = "MIRRORHUB_IMPORTER_UI_HOSTS"
KEEP_RUNS = 50  # runs shown (and kept in memory)
KEEP_FILES = 200  # run records kept in the runs directory
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
    -1: "web UI stopped during the run",
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
    log: Path | None = None  # output file in the runs directory

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def append(self, text: str) -> None:
        with self.lock:
            self.output += text
            if len(self.output) > MAX_OUTPUT:
                self.output = "[… older output dropped …]\n" + self.output[-MAX_OUTPUT:]
            if self.log is not None:
                try:
                    with self.log.open("a", encoding="utf-8") as handle:
                        handle.write(text)
                except OSError:
                    self.log = None  # keep running without the file

    def state(self) -> str:
        if self.running:
            return "running"
        if self.exit_code == 11:
            return "differs"  # finished, but the tree differs from files.json
        return "ok" if self.exit_code == 0 else "failed"

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "argv": self.argv,
            "started": self.started,
            "finished": self.finished,
            "exit_code": self.exit_code,
        }

    def plan(self) -> tuple[str, int, int, int] | None:
        """(export directory, added, changed, deleted) of a finished --dry-run --rebase
        run of one export directory, from its summary line; else None."""
        if self.exit_code != 0 or "--dry-run" not in self.argv or "--rebase" not in self.argv:
            return None
        if "--export" not in self.argv:
            return None
        export = self.argv[self.argv.index("--export") + 1]
        with self.lock:
            match = PLAN_RE.search(self.output)
        if match is None:
            return None
        return export, int(match["added"]), int(match["changed"]), int(match["deleted"])


PLAN_RE = re.compile(
    r"^dry-run \S+: (?P<added>\d+) added, (?P<changed>\d+) changed, (?P<deleted>\d+) deleted",
    re.M,
)


class Runner:
    """Starts importer commands as child processes, one at a time.

    Every run is kept as <runs_dir>/<id>.json (command, times, exit code) and
    <id>.log (output), so the list and the output survive a restart of the UI.
    """

    def __init__(self, config_path: Path, runs_dir: Path | None = None) -> None:
        self.config_path = config_path
        self.runs_dir = runs_dir
        self.runs: OrderedDict[int, Run] = OrderedDict()
        self._next = 1
        self._lock = threading.Lock()
        root = Path(mirrorhub.__file__).resolve().parents[1]  # src dir or the .pyz
        self._env = dict(os.environ)
        self._env["PYTHONPATH"] = os.pathsep.join(
            [str(root), *filter(None, [os.environ.get("PYTHONPATH")])]
        )
        self._env["PYTHONUNBUFFERED"] = "1"
        self._env[events.TRIGGER_ENV] = "ui"
        self._load()

    def _load(self) -> None:
        if self.runs_dir is None or not self.runs_dir.is_dir():
            return
        files = sorted(
            (path for path in self.runs_dir.glob("*.json") if path.stem.isdigit()),
            key=lambda path: int(path.stem),
        )
        for path in files[:-KEEP_FILES] if len(files) > KEEP_FILES else []:
            path.unlink(missing_ok=True)
            path.with_suffix(".log").unlink(missing_ok=True)
        for path in files[-KEEP_RUNS:]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                run = Run(
                    int(data["id"]),
                    str(data["title"]),
                    [str(item) for item in data["argv"]],
                    float(data["started"]),
                    data.get("finished"),
                    data.get("exit_code"),
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
            log = path.with_suffix(".log")
            try:
                run.output = log.read_text(encoding="utf-8", errors="replace")[-MAX_OUTPUT:]
            except OSError:
                run.output = ""
            if run.exit_code is None:  # the UI stopped while it ran
                run.exit_code, run.finished = -1, run.started
                run.output += "\n# the web UI stopped during this run; its result is unknown\n"
                self._save(run)
            self.runs[run.id] = run
        if files:
            self._next = int(files[-1].stem) + 1

    def _save(self, run: Run) -> None:
        if self.runs_dir is None:
            return
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            target = self.runs_dir / f"{run.id}.json"
            temporary = self.runs_dir / f".{run.id}.json.tmp"
            temporary.write_text(json.dumps(run.metadata()), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            pass  # the run itself does not depend on its record

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
            if self.runs_dir is not None:
                run.log = self.runs_dir / f"{run.id}.log"
            self._save(run)
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
        code = process.wait()
        meaning = EXIT_CODES.get(code, "")
        run.append(f"\n# exit code {code}" + (f" ({meaning})" if meaning else "") + "\n")
        run.finished = time.time()
        run.exit_code = code
        self._save(run)

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


PLAN_MAX_AGE_S = 3600


def _planned_rebase(form: dict[str, list[str]], export: str, runner: Runner | None) -> int:
    """A rebase needs a finished dry run with --rebase of the same export, not older
    than an hour, so the user has seen how many paths it deletes. Returns its id."""
    plan_id = _one(form, "plan")
    run = runner.runs.get(int(plan_id)) if runner is not None and plan_id.isdigit() else None
    plan = run.plan() if run is not None else None
    if run is None or plan is None or plan[0] != export:
        raise FormError(
            "a rebase needs a dry run of this export first: open the export and use "
            "'Plan rebase (dry run)'"
        )
    if time.time() - (run.finished or 0) > PLAN_MAX_AGE_S:
        raise FormError("the rebase plan is older than an hour; plan it again")
    return run.id


def build_command(
    form: dict[str, list[str]], config: Config, runner: Runner | None = None
) -> tuple[str, list[str]]:
    """Title and CLI arguments for a submitted form; raises FormError.

    Difference lists of imports and checks go to the reports directory (shown on
    the repository's report page) unless the form names another directory.
    """
    reports = webdata.reports_dir(config)
    automatic_reports = config.reports_dir is None  # else the CLI writes them itself
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
        if _flag(form, "rebase"):
            if scope != "export":
                raise FormError("--rebase works on one export directory (open the export)")
            if _flag(form, "dry_run"):
                title += " (rebase plan)"
            else:
                plan_id = _planned_rebase(form, args[args.index("--export") + 1], runner)
                title += f" (rebase, plan #{plan_id})"
            args.append("--rebase")
        if _flag(form, "discard_staging"):
            if scope == "all":
                raise FormError("--discard-staging cannot be used with all repositories")
            if not _flag(form, "confirm"):
                raise FormError("tick the confirmation to use --discard-staging")
            args.append("--discard-staging")
        drift = _one(form, "drift_output")
        if drift:
            args += ["--drift-output", _absolute(drift, "drift output directory")]
        elif automatic_reports and not _flag(form, "dry_run"):
            args += ["--drift-output", str(reports)]
        if _flag(form, "dry_run") and not _flag(form, "rebase"):
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
        elif automatic_reports:
            args += ["--output-dir", str(reports)]
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
header nav { display: flex; gap: 0.9rem; }
header nav a { font-weight: 500; color: var(--accent); }
table.facts th { width: 13rem; color: var(--muted); font-weight: 500; }
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
button, a.button { font: inherit; padding: 0.42rem 0.9rem; border-radius: 6px;
  cursor: pointer; border: 1px solid var(--accent); background: var(--accent);
  color: var(--surface); font-weight: 600; margin-top: 0.75rem; display: inline-block;
  text-decoration: none; }
button.secondary, a.button.secondary { background: transparent; color: var(--accent); }
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
  table.stack td { display: block; border: 0; padding: 0.2rem 0; }
  table.stack td[data-label]::before { content: attr(data-label); display: block;
    color: var(--muted); font-size: 0.8rem; }
  table.stack td, .runs td:nth-child(2) { overflow-wrap: anywhere; }
  .runs td:not(:nth-child(2)) { white-space: nowrap; }
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


def _ago(value: object) -> str:
    """'3.0 h ago' for an ISO time, '' if unknown."""
    moment = webdata.parse_time(value)
    if moment is None:
        return ""
    seconds = max(0.0, time.time() - moment.timestamp())
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _size(count: int) -> str:
    if count >= 1024**3:
        return f"{count / 1024**3:.2f} GiB"
    return f"{count / 1024**2:.1f} MiB"


REPORT_HELP = {
    "missing": "listed in files.json, not in the tree",
    "extra": "in the tree, not in files.json",
    "type": "file where a symlink is expected or the other way round",
    "size": "size differs from files.json",
    "mtime": "modification time differs from files.json",
    "symlink target": "symlink points elsewhere",
    "sha256": "content differs (SHA-256 check)",
}


def _duration(run: Run) -> str:
    seconds = round((run.finished or time.time()) - run.started)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Page:
    def __init__(self, ui: WebUI, nonce: str) -> None:
        self.ui = ui
        self.nonce = nonce

    def layout(self, title: str, body: str, *, nav: bool = True) -> str:
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{esc(title)} - MirrorHub importer</title><style>{STYLE}</style></head><body>"
            "<header><a href='/'>MirrorHub importer</a>"
            + (self._nav() if nav else "")
            + "</header><main>"
            + body
            + "</main></body></html>"
        )

    def _nav(self) -> str:
        """Navigation and build/config line; not on the login page."""
        return (
            "<nav><a href='/'>Repositories</a><a href='/history'>History</a></nav>"
            f"<span class='muted'>{esc(buildinfo.describe())} · "
            f"config {esc(self.ui.config_path)}</span>"
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
        status = self.ui.events.repo_status()
        body = "<h1>Repositories</h1>"
        if error:
            body += f"<div class='alert'>{esc(error)}</div>"
        active = self.ui.runner.active()
        if active is not None:
            body += (
                f"<div class='card'>Running: <a href='/runs/{active.id}'>{esc(active.title)}</a> "
                f"since {_time(active.started)}</div>"
            )
        if config.events_dir is None:
            body += (
                "<div class='card muted'>Set <code>events_dir</code> in importer.toml to see "
                "import results, tree checks and timer runs here and on the History page.</div>"
            )
        table = (
            "<div class='card scroll'><table class='stack'><thead><tr><th>Repository</th>"
            "<th>Last import</th><th>Tree</th><th>Pending</th><th>Actions</th></tr></thead><tbody>"
        )
        for row in rows:
            table += self._repo_row(row, status.get(row.slug, webdata.RepoStatus()))
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

    def _repo_row(self, row: RepoRow, status: webdata.RepoStatus) -> str:
        slug = esc(row.slug)
        pending = "<span class='muted'>0</span>"
        if row.pending:
            links = ", ".join(
                f"<a href='/repos/{slug}/exports/{esc(name)}'>{esc(name)}</a>"
                for name in row.pending[:3]
            )
            more = " …" if len(row.pending) > 3 else ""
            pending = f"<span class='pending'>{len(row.pending)}</span> {links}{more}"
        last = "<span class='muted'>none</span>"
        if row.last_export_id:
            last = (
                f"<a href='/repos/{slug}/exports/{esc(row.last_export_id)}'>"
                f"<code>{esc(row.last_export_id)}</code></a><br>"
                f"<span class='muted'>{esc(_local(row.applied_at))}</span>"
            )
        if row.error:
            last = f"<span class='state failed'>{esc(row.error)}</span>"
        elif status.failed_last() and status.import_failed is not None:
            failed = status.import_failed
            last += (
                f"<br><span class='state failed'>failed {esc(_ago(failed.get('time')))}</span> "
                f"<span class='muted'>{esc(failed.get('error_kind'))}: "
                f"{esc(str(failed.get('error'))[:120])}</span>"
            )
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
                self.quick("check", {"slug": row.slug, "hash": "on"}, "Check SHA-256", "secondary"),
            ]
        )
        return (
            f"<tr><td><strong>{slug}</strong><br><span class='muted'>"
            f"{row.applied} applied, {row.superseded} superseded</span><br>"
            f"<a href='/history?slug={slug}'>history</a></td>"
            f"<td data-label='Last import'>{last}</td>"
            f"<td data-label='Tree'>{self._tree_cell(row.slug, status)}</td>"
            f"<td data-label='Pending'>{pending}</td>"
            f"<td><div class='row-actions'>{actions}</div></td></tr>"
        )

    def _tree_cell(self, slug: str, status: webdata.RepoStatus) -> str:
        report = webdata.read_report(self.ui.config, slug, limit=0)
        tree = status.tree
        link = f"<a href='/repos/{esc(slug)}/report'>report</a>" if report else ""
        if tree is None and report is None:
            return "<span class='muted'>not checked yet</span>"
        if tree is not None:
            differences = int(tree.get("differences") or 0)
            kind = "SHA-256" if tree.get("hashed") else "quick"
            when = f"{kind} {_ago(tree.get('time'))}"
            raw = tree.get("counts")
            counts = raw if isinstance(raw, dict) else {}
        else:
            assert report is not None
            differences = report.total
            kind = "SHA-256" if "sha256" in report.summary.get("check", "") else "quick"
            when = f"{kind} {_ago(report.summary.get('checked_at'))}"
            counts = {k: v for k, v in report.summary.items() if k in webdata.REPORT_KINDS}
        if differences:
            listed = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()) if str(v) != "0")
            text = (
                f"<span class='state differs'>{_plural(differences, 'difference')}</span> "
                f"<span class='muted'>({esc(listed)})</span>"
            )
        else:
            text = "<span class='state ok'>matches</span>"
        hashed = status.tree_hashed
        sha = f"SHA-256 {_ago(hashed.get('time'))}" if hashed else "no SHA-256 check yet"
        detail = when if when.startswith("SHA-256") else f"{when} · {sha}"
        return f"{text}<br><span class='muted'>{esc(detail)}</span> {link}"

    # -- History, report, export pages

    def history(self, slug: str | None) -> str:
        found = self.ui.events.history(slug)
        title = f"History of {slug}" if slug else "History"
        body = f"<h1>{esc(title)}</h1>"
        if self.ui.config.events_dir is None:
            body += (
                "<div class='alert'>No <code>events_dir</code> in importer.toml: the importer "
                "writes no event files, so there is no history beyond the runs of this UI."
                "</div>"
            )
        filters = "".join(
            f"<a href='/history?slug={esc(name)}'>{esc(name)}</a> "
            for name in sorted(
                {str(e.get("repo")) for e in self.ui.events.events() if e.get("repo")}
            )
        )
        body += (
            "<div class='card'><p class='muted'>Imports, failures, tree checks and every "
            "import run, from the event files (started from this UI: <em>ui</em>, by a "
            "timer or the shell: <em>cli</em>). Newest first, at most 300 entries.</p>"
            f"<p><a href='/history'>all</a> {filters}</p></div>"
        )
        rows = "".join(self._history_row(event) for event in found)
        if not rows:
            rows = "<tr><td colspan='5' class='muted'>No events.</td></tr>"
        body += (
            "<div class='card scroll'><table class='stack'><thead><tr><th>Time</th>"
            "<th>Event</th><th>Repository</th><th>Export</th><th>Details</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
        return self.layout(title, body)

    def _history_row(self, event: dict[str, Any]) -> str:
        kind = str(event.get("event"))
        repo = str(event.get("repo") or "")
        export = str(event.get("export_id") or "")
        state = {"import_failed": "failed", "check_failed": "failed"}.get(kind, "")
        details = ""
        if kind == "import_applied":
            details = (
                f"{event.get('export_mode')}, {event.get('payload_count')} files, "
                f"{event.get('deleted_count')} deleted, {event.get('duration_s')} s"
            )
        elif kind in ("import_failed", "check_failed"):
            details = (
                f"{event.get('error_kind')} (exit {event.get('exit_code')}): {event.get('error')}"
            )
        elif kind == "import_superseded":
            details = f"superseded by {event.get('superseded_by') or 'the receiver state'}"
        elif kind == "tree_check":
            differences = int(event.get("differences") or 0)
            state = "differs" if differences else "ok"
            check = "SHA-256" if event.get("hashed") else "quick"
            details = f"{check} ({event.get('origin')}): {_plural(differences, 'difference')}"
        elif kind == "import_run":
            code = event.get("exit_code")
            state = "ok" if code == 0 else "differs" if code == 11 else "failed"
            meaning = EXIT_CODES.get(code, "") if isinstance(code, int) else ""
            details = f"{event.get('scope')}: exit {code} {meaning}, {event.get('duration_s')} s"
        elif kind == "import_adopted":
            details = "state written by adopt"
        repo_cell = f"<a href='/history?slug={esc(repo)}'>{esc(repo)}</a>" if repo else ""
        export_cell = (
            f"<a href='/repos/{esc(repo)}/exports/{esc(export)}'><code>{esc(export)}</code></a>"
            if repo and export
            else ""
        )
        label = f"<span class='state {state}'>{esc(kind)}</span>" if state else esc(kind)
        return (
            f"<tr><td>{esc(_local(str(event.get('time') or '')))}<br>"
            f"<span class='muted'>{esc(event.get('trigger') or 'cli')}</span></td>"
            f"<td data-label='Event'>{label}</td><td data-label='Repository'>{repo_cell}</td>"
            f"<td data-label='Export'>{export_cell}</td>"
            f"<td data-label='Details'>{esc(details)}</td></tr>"
        )

    def report(self, slug: str) -> str:
        report = webdata.read_report(self.ui.config, slug)
        title = f"Tree report of {slug}"
        body = f"<h1>{esc(title)}</h1>"
        if report is None:
            body += (
                "<div class='card'>No report yet. Reports are written by every check and "
                "every import tree check started here, and by timer runs if "
                f"<code>reports_dir</code> is set (directory: "
                f"<code>{esc(webdata.reports_dir(self.ui.config))}</code>).</div>"
            )
            return self.layout(title, body)
        summary = report.summary
        body += (
            "<div class='card'><p>"
            + (
                f"<span class='state differs'>{_plural(report.total, 'difference')}</span>"
                if report.total
                else "<span class='state ok'>no differences</span>"
            )
            + f" from <code>{esc(summary.get('export_id'))}</code>, "
            f"{esc(summary.get('check'))} check {esc(_ago(summary.get('checked_at')))} "
            f"({esc(_local(summary.get('checked_at', '')))})</p>"
            "<p class='muted'>A report is replaced by the next check. Files changed on the "
            "receiver outside the importer show up here; the repair request below fixes "
            "them without a full export.</p>"
            + self.quick("check", {"slug": slug}, "Check again", "secondary")
            + self.quick("check", {"slug": slug, "hash": "on"}, "Check SHA-256", "secondary")
            + "</div>"
        )
        found = webdata.repair_request(self.ui.config, slug)
        if found is not None:
            path, document = found
            name = esc(path.name)
            body += (
                "<div class='card'><h2>Repair request</h2>"
                f"<p>{esc(repair.count(document))}, based on "
                f"<code>{esc(document.get('base_export_id'))}</code>.</p>"
                "<p class='muted'>Download both files and carry them to the sender: "
                "<em>Exports</em> page of the repository, <em>Repair export</em>. The repair "
                "export brings the missing and differing files and removes the extra ones; "
                "once it is imported the tree matches again and this request disappears. "
                "Import no other export of this repository before it, or the request no "
                "longer fits (check again for a new one).</p>"
                f"<p><a class='button' href='/repos/{esc(slug)}/report/{name}'>{name}</a> "
                f"<a class='button secondary' href='/repos/{esc(slug)}/report/{name}.sha256'>"
                f"{name}.sha256</a></p></div>"
            )
        for kind, lines in report.kinds.items():
            count = report.truncated.get(kind, len(lines))
            if not count:
                continue
            shown = f" (first {len(lines)} shown)" if kind in report.truncated else ""
            download = (
                f"<a href='/repos/{esc(slug)}/report/{esc(kind.replace(' ', '-'))}.txt'>"
                "download</a>"
            )
            body += (
                f"<div class='card'><h2>{esc(kind)}: {count}{shown}</h2>"
                f"<p class='muted'>{esc(REPORT_HELP.get(kind, ''))} · {download}</p>"
                f"<pre class='out'>{esc(chr(10).join(lines))}</pre></div>"
            )
        return self.layout(title, body)

    def export(self, info: webdata.ExportInfo) -> str:
        slug, export_id = esc(info.slug), esc(info.export_id)
        counts = info.counts
        state = {"applied": "ok", "superseded": "muted", "pending": "differs"}[info.status]
        facts = [
            ("Status", f"<span class='state {state}'>{esc(info.status)}</span>"),
            ("Mode", esc(info.mode)),
            ("Base export", esc(info.manifest.get("base_export_id") or "—")),
            ("Fits the receiver", esc(info.chain)),
            ("Created", esc(_local(str(info.manifest.get("created_at") or "")))),
            ("Files in the tree", esc(counts.get("inventory", "?"))),
            ("Files in this export", esc(counts.get("payload", "?"))),
            ("Deletions", esc(counts.get("deleted", "?"))),
            (
                "Archive",
                f"{esc(info.manifest.get('archive_format'))}, {info.parts} part(s), "
                f"{_size(info.archive_bytes)}",
            ),
            ("Signed", "yes (MANIFEST.sha256.sig)" if info.signed else "no"),
            ("Sender commit", esc(info.sender_commit or "unknown")),
            ("Published", "yes (.done)" if info.published else "no (.done missing)"),
            ("Directory", f"<code>{esc(info.directory)}</code>"),
        ]
        rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in facts)
        body = f"<h1>Export {export_id}</h1><p><a href='/history?slug={slug}'>{slug}</a></p>"
        if info.error:
            body += f"<div class='alert'>{esc(info.error)}</div>"
        body += (
            f"<div class='card scroll'><table class='facts'>{rows}</table>"
            "<p class='muted'>Shown from manifest.json as found in the inbox; the import "
            "verifies checksums, signature and contents before anything is changed.</p></div>"
        )
        actions = ""
        export_dir = str(info.directory)
        if info.status == "pending" and info.needs_rebase:
            actions = (
                "<p>This full export replaces the repository's tree: files that are not in "
                "its files.json are deleted. Plan it first; the plan lists every change and "
                "the run page then offers to apply it.</p>"
                + self.quick(
                    "import",
                    {"scope": "export", "export": export_dir, "dry_run": "on", "rebase": "on"},
                    "Plan rebase (dry run)",
                )
            )
        elif info.importable:
            actions = self.quick(
                "import", {"scope": "export", "export": export_dir}, "Import this export"
            ) + self.quick(
                "import",
                {"scope": "export", "export": export_dir, "dry_run": "on"},
                "Dry run",
                "secondary",
            )
        elif info.status == "pending":
            actions = (
                "<p class='muted'>Not importable on its own right now (see above). "
                "<em>Import</em> on the repository applies pending exports in order.</p>"
            )
        if actions:
            body += (
                f"<div class='card'><h2>Actions</h2><div class='row-actions'>{actions}</div></div>"
            )
        return self.layout(f"Export {info.export_id}", body)

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
            "<label class='check'><input type='checkbox' name='discard_staging'> "
            "<code>--discard-staging</code>: drop another unfinished import</label>"
            "<label class='check'><input type='checkbox' name='confirm'> I understand</label>"
            "<p class='muted'><code>--rebase</code>: open the full export (link in the "
            "repository table) and use <em>Plan rebase</em>.</p>"
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
        plan = run.plan()
        plan_box = ""
        if plan is not None:
            export, added, changed, deleted = plan
            apply = self.quick(
                "import",
                {"scope": "export", "export": export, "rebase": "on", "plan": str(run.id)},
                f"Apply rebase ({deleted} deletions)",
            )
            plan_box = (
                "<div class='card'><h2>Rebase plan</h2>"
                f"<p><strong>{added}</strong> added, <strong>{changed}</strong> changed, "
                f"<strong class='state failed'>{deleted}</strong> deleted "
                "(lines starting with <code>D</code> below).</p>"
                "<p class='muted'>Valid for an hour. Applying runs the same export with "
                "<code>--rebase</code>.</p>"
                f"<div class='row-actions'>{apply}</div></div>"
            )
        with run.lock:
            output = run.output
        body = (
            f"<h1>Run #{run.id}: {esc(run.title)}</h1><div class='card'>"
            f"<p><code>{esc(command)}</code></p><p>Started {_time(run.started)}, "
            f"<span id='duration'>{_duration(run)}</span> · <span id='result'>{result}</span></p>"
            f"{cancel}</div>{plan_box}"
            f"<div class='card'><pre class='out' id='out'>{esc(output)}</pre></div>"
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
            nav=False,
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
        self.runner = Runner(self.config_path, config.state_dir / "ui-runs")
        self.events = webdata.EventIndex(config.events_dir)

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

    def _text(self, status: int, text: str) -> None:
        self._send(status, text, content_type="text/plain")

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
        if parts == ["history"]:
            slug = (parse_qs(url.query).get("slug") or [""])[0]
            page = self._page()
            self._send(
                HTTPStatus.OK,
                page.history(slug if SLUG_RE.fullmatch(slug) else None),
                nonce=page.nonce,
            )
            return
        if len(parts) >= 3 and parts[0] == "repos" and SLUG_RE.fullmatch(parts[1]):
            self._repo_page(parts[1], parts[2:])
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

    def _repo_page(self, slug: str, rest: list[str]) -> None:
        page = self._page()
        if rest == ["report"]:
            self._send(HTTPStatus.OK, page.report(slug), nonce=page.nonce)
            return
        if len(rest) == 2 and rest[0] == "report" and rest[1].startswith("repair-"):
            path = webdata.repair_file(self.ui.config, slug, rest[1])
            if path is not None:
                self._send(
                    HTTPStatus.OK,
                    path.read_text(encoding="ascii"),
                    content_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{rest[1]}"'},
                )
                return
        if len(rest) == 2 and rest[0] == "report" and rest[1].endswith(".txt"):
            kind = rest[1].removesuffix(".txt").replace("-", " ")
            path = webdata.report_file(self.ui.config, slug, kind)
            if path is not None:
                self._send(
                    HTTPStatus.OK,
                    path.read_text(encoding="utf-8", errors="replace"),
                    content_type="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{slug}-{rest[1]}"'},
                )
                return
        if len(rest) == 2 and rest[0] == "exports":
            try:
                state = StateStore(self.ui.config.state_dir, slug).load()
            except ImporterError as exc:
                self._text(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                return
            info = webdata.export_info(self.ui.config, slug, rest[1], state)
            if info is not None:
                self._send(HTTPStatus.OK, page.export(info), nonce=page.nonce)
                return
        self._text(HTTPStatus.NOT_FOUND, "not found")

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
                title, args = build_command(form, self.ui.config, self.ui.runner)
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
