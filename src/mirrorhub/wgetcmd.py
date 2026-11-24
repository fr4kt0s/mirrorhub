"""The wget command of a directory-tree mirror: generate, show, parse and check it.

The repository form shows the complete command. An operator may switch to editing it by
hand; the edited options are then stored in ``Repository.wget_custom_args`` and used
instead of the generated ones. MirrorHub keeps control of what decides where files go
and how a run behaves:

- the URL (from host, root path and method),
- ``--directory-prefix`` (the repository's directory below the data mount),
- ``--timestamping`` for a sync and ``--spider`` for a dry run.

Every other option must be on the allowlist below. Options that start programs, read or
write arbitrary files, send local data or leave the upstream host (``--use-askpass``,
``--execute``, ``--output-document``, ``--post-file``, ``--span-hosts`` …) are not on
it. Only the long form is accepted (``--recursive``, not ``-r``) so the check stays
unambiguous.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from mirrorhub import archfilter

MAX_ARGS = 60


@dataclass(frozen=True)
class Opt:
    kind: str  # flag | int | level | rate | choice | choices | regex | pattern
    lo: int = 0
    hi: int = 0
    values: frozenset[str] = frozenset()
    pattern: str = ""


def _flag() -> Opt:
    return Opt("flag")


def _int(lo: int, hi: int) -> Opt:
    return Opt("int", lo=lo, hi=hi)


ALLOWED: dict[str, Opt] = {
    # recursion and layout
    "--recursive": _flag(),
    "--mirror": _flag(),
    "--level": Opt("level", lo=0, hi=100),
    "--no-parent": _flag(),
    "--no-host-directories": _flag(),
    "--cut-dirs": _int(0, 50),
    "--force-directories": _flag(),
    "--protocol-directories": _flag(),
    "--relative": _flag(),
    "--page-requisites": _flag(),
    "--adjust-extension": _flag(),
    "--convert-links": _flag(),
    "--trust-server-names": _flag(),
    "--restrict-file-names": Opt(
        "choices",
        values=frozenset({"unix", "windows", "nocontrol", "ascii", "lowercase", "uppercase"}),
    ),
    "--no-remove-listing": _flag(),
    # filters
    "--accept": Opt("pattern", pattern=r"[A-Za-z0-9.,*?_\[\]-]{1,300}"),
    "--reject": Opt("pattern", pattern=r"[A-Za-z0-9.,*?_\[\]-]{1,300}"),
    "--accept-regex": Opt("regex"),
    "--reject-regex": Opt("regex"),
    "--regex-type": Opt("choice", values=frozenset({"posix", "pcre"})),
    "--include-directories": Opt("pattern", pattern=r"[A-Za-z0-9/,._*?-]{1,500}"),
    "--exclude-directories": Opt("pattern", pattern=r"[A-Za-z0-9/,._*?-]{1,500}"),
    "--ignore-case": _flag(),
    # network behaviour
    "--tries": _int(1, 50),
    "--timeout": _int(1, 3600),
    "--dns-timeout": _int(1, 3600),
    "--connect-timeout": _int(1, 3600),
    "--read-timeout": _int(1, 3600),
    "--wait": _int(0, 300),
    "--waitretry": _int(0, 600),
    "--random-wait": _flag(),
    "--limit-rate": Opt("rate"),
    "--retry-connrefused": _flag(),
    "--inet4-only": _flag(),
    "--inet6-only": _flag(),
    "--prefer-family": Opt("choice", values=frozenset({"IPv4", "IPv6", "none"})),
    "--no-dns-cache": _flag(),
    "--no-cache": _flag(),
    "--no-cookies": _flag(),
    "--no-http-keep-alive": _flag(),
    "--ignore-length": _flag(),
    "--no-iri": _flag(),
    "--user-agent": Opt("pattern", pattern=r"[A-Za-z0-9 ._/()+;:,-]{1,120}"),
    # output to the job log
    "--no-verbose": _flag(),
    "--verbose": _flag(),
    "--quiet": _flag(),
    "--continue": _flag(),
}

MANAGED = {
    "--directory-prefix": "set by MirrorHub to the repository directory",
    "--timestamping": "added by MirrorHub for every sync",
    "--spider": "added by MirrorHub for dry runs",
}

_RATE_RE = re.compile(r"[1-9][0-9]{0,8}[km]?")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CommandError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


def tree_url(method: str, host: str, root: str) -> str:
    root = root.strip("/")
    return f"{method}://{host}/{root}/" if root else f"{method}://{host}/"


def generated_args(
    *,
    upstream_root: str,
    include_regex: str = "",
    exclude_regex: str = "",
    keep_arches: Iterable[str] = (),
    bwlimit_kbps: int = 0,
    extra_args: Iterable[str] = (),
) -> list[str]:
    """Options MirrorHub generates from the form fields (without URL and managed options)."""
    root = upstream_root.strip("/")
    depth = len(root.split("/")) if root else 0
    args = [
        "--recursive",
        # wget stops at depth 5 by default; package pools are often deeper
        # (dists/<suite>/pool/<channel>/<arch>/<file>). --no-parent keeps the walk
        # below the upstream root.
        "--level=inf",
        "--no-parent",
        "--no-host-directories",
        f"--cut-dirs={depth}",
        "--no-verbose",
        "--tries=3",
        "--timeout=60",
    ]
    reject = archfilter.combine_reject(exclude_regex, list(keep_arches))
    if include_regex or reject:
        args.append("--regex-type=posix")
    if include_regex:
        args.append(f"--accept-regex={include_regex}")
    if reject:
        args.append(f"--reject-regex={reject}")
    if bwlimit_kbps:
        args.append(f"--limit-rate={bwlimit_kbps}k")
    args.extend(extra_args)
    return args


def full_argv(args: Iterable[str], *, target: str, url: str, dry_run: bool) -> list[str]:
    """The argv MirrorHub runs: options, then the managed options, then the URL."""
    return [
        "wget",
        *args,
        f"--directory-prefix={target}",
        "--spider" if dry_run else "--timestamping",
        url,
    ]


def display(args: Iterable[str], *, target: str, url: str) -> str:
    """One shell-quoted line, as shown in the form (sync, not dry run)."""
    return shlex.join(full_argv(args, target=target, url=url, dry_run=False))


def _check_value(name: str, opt: Opt, value: str) -> str | None:
    if _CONTROL_RE.search(value):
        return f"{name}: control characters are not allowed"
    if opt.kind == "int":
        if not value.isdigit() or not opt.lo <= int(value) <= opt.hi:
            return f"{name}: integer {opt.lo}-{opt.hi} required"
    elif opt.kind == "level":
        if value != "inf" and (not value.isdigit() or not opt.lo <= int(value) <= opt.hi):
            return f"{name}: 'inf' or an integer {opt.lo}-{opt.hi} required"
    elif opt.kind == "rate":
        if not _RATE_RE.fullmatch(value):
            return f"{name}: a number with optional k or m suffix required (e.g. 2048k)"
    elif opt.kind == "choice":
        if value not in opt.values:
            return f"{name}: one of {', '.join(sorted(opt.values))}"
    elif opt.kind == "choices":
        parts = value.split(",")
        if not value or any(part not in opt.values for part in parts):
            return f"{name}: comma-separated list of {', '.join(sorted(opt.values))}"
    elif opt.kind == "regex":
        if not value or len(value) > 1000:
            return f"{name}: 1-1000 characters required"
    elif opt.kind == "pattern" and not re.fullmatch(opt.pattern, value):
        return f"{name}: invalid value {value[:40]!r}"
    return None


def check_option(name: str, value: str | None) -> str | None:
    """Error for one option, or None. ``value`` is None when the option has none."""
    if name in MANAGED:
        return f"{name} is {MANAGED[name]}; remove it from the command"
    opt = ALLOWED.get(name)
    if opt is None:
        return f"{name} is not allowed (see the operations handbook, section 4.6)"
    if opt.kind == "flag":
        return f"{name} takes no value" if value is not None else None
    if value is None:
        return f"{name} requires a value"
    return _check_value(name, opt, value)


def validate_args(args: Iterable[str]) -> str | None:
    """Check stored options (``--name`` or ``--name=value``). Error message or None."""
    items = list(args)
    if len(items) > MAX_ARGS:
        return f"wget command: at most {MAX_ARGS} options"
    for item in items:
        if not isinstance(item, str) or not item.startswith("--"):
            return f"wget command: malformed option {str(item)[:40]!r}"
        name, sep, value = item.partition("=")
        error = check_option(name, value if sep else None)
        if error:
            return f"wget command: {error}"
    return None


def parse_command(text: str, *, url: str, target: str) -> list[str]:
    """Options from an edited command line, normalised to ``--name[=value]``.

    The line must start with ``wget``. The URL may be left out; if present it must be
    the repository's URL. ``--directory-prefix=<target>`` and ``--timestamping`` as shown
    in the form are accepted and dropped (MirrorHub adds them itself); any other value of
    a managed option is an error. Raises CommandError with all problems found.
    """
    try:
        tokens = shlex.split(text.replace("\\\r\n", " ").replace("\\\n", " "))
    except ValueError as exc:
        raise CommandError([f"wget command: {exc}"]) from exc
    if not tokens or tokens[0] != "wget":
        raise CommandError(["wget command: must start with 'wget'"])

    errors: list[str] = []
    args: list[str] = []
    positionals: list[str] = []
    rest = tokens[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        index += 1
        if token.startswith("--"):
            name, sep, value = token.partition("=")
            if name == "--timestamping" and not sep:
                continue
            if name == "--directory-prefix":
                if not sep and index < len(rest):
                    value, sep = rest[index], "="
                    index += 1
                if sep and value.rstrip("/") == target.rstrip("/"):
                    continue
            opt = ALLOWED.get(name)
            needs_value = opt is not None and opt.kind != "flag"
            if needs_value and not sep and index < len(rest) and not rest[index].startswith("-"):
                value, sep = rest[index], "="
                index += 1
            error = check_option(name, value if sep else None)
            if error:
                errors.append(f"wget command: {error}")
            else:
                args.append(f"{name}={value}" if sep else name)
        elif token.startswith("-") and len(token) > 1:
            errors.append(
                f"wget command: use the long form instead of {token[:20]!r} "
                "(e.g. --recursive instead of -r)"
            )
        else:
            positionals.append(token)

    if len(positionals) > 1 or (positionals and positionals[0] != url):
        errors.append(
            f"wget command: the only URL allowed is {url} (change host, root path or "
            "method above to change it)"
        )
    if len(args) > MAX_ARGS:
        errors.append(f"wget command: at most {MAX_ARGS} options")
    if errors:
        raise CommandError(errors)
    return args
