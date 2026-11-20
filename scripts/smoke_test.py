#!/usr/bin/env python3
"""Smoke tests for a running MirrorHub deployment.

Usage:
    python3 scripts/smoke_test.py --base-url https://localhost:8443 --insecure
    python3 scripts/smoke_test.py --base-url https://mirrorhub.example.com \
        --ca-bundle /path/to/rootCA.pem

Exit code is 0 only when every required check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
ALLOWED_SCHEMES = frozenset({"http", "https"})
HTTPS_ONLY_BODY = "HTTP request to an HTTPS server"


class CheckError(Exception):
    """Raised when a check cannot complete."""


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


@dataclass
class TlsOptions:
    """TLS and virtual-host settings shared by every HTTP check."""

    insecure: bool = False
    ca_bundle: str | None = None
    host_header: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses instead of following them."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _checked_url(url: str) -> str:
    """Reject anything that is not a plain HTTP(S) URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise CheckError(f"refusing non-HTTP URL: {url}")
    if not parsed.netloc:
        raise CheckError(f"URL has no host: {url}")
    return url


def _ssl_context(options: TlsOptions) -> ssl.SSLContext:
    try:
        context = (
            ssl.create_default_context(cafile=options.ca_bundle)
            if options.ca_bundle
            else ssl.create_default_context()
        )
    except FileNotFoundError as error:
        raise CheckError(f"CA bundle not found: {error.filename}") from error
    if options.insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _opener(options: TlsOptions, follow: bool) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPSHandler(context=_ssl_context(options)),
    ]
    if not follow:
        handlers.append(_NoRedirect())
    return urllib.request.build_opener(*handlers)


def _get(
    url: str,
    options: TlsOptions,
    timeout: int = 10,
    follow: bool = True,
    upgrade: bool = True,
) -> tuple[int, str]:
    url = _checked_url(url)
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url,
        headers={"User-Agent": "mirrorhub-smoke"},
    )
    if options.host_header:
        request.add_unredirected_header("Host", options.host_header)

    try:
        with _opener(options, follow).open(request, timeout=timeout) as response:
            return response.status, response.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body = error.read(200_000).decode("utf-8", "replace")
        if upgrade and error.code == 400 and HTTPS_ONLY_BODY in body and url.startswith("http://"):
            return _get(
                "https://" + url[len("http://") :],
                options,
                timeout,
                follow,
                upgrade=False,
            )
        return error.code, body
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise CheckError(
                f"TLS verification failed ({reason.verify_message}); use --insecure or --ca-bundle"
            ) from error
        if isinstance(reason, ssl.SSLError):
            raise CheckError(f"TLS handshake failed: {reason}") from error
        raise CheckError(f"{url}: {reason}") from error


def _run(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - resolved paths, literal args
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise CheckError(f"missing executable: {error.filename}") from error
    except subprocess.TimeoutExpired as error:
        joined = " ".join(argv[:3])
        raise CheckError(f"timed out after {timeout}s: {joined}") from error


def _compose_files() -> list[str]:
    files: list[str] = []
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        if os.path.exists(name):
            files.extend(["-f", name])
            break
    if os.path.exists("docker-compose.override.yml"):
        files.extend(["-f", "docker-compose.override.yml"])
    return files


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        raise CheckError("docker executable not found in PATH")
    return path


def _compose_prefix() -> list[str]:
    return [_docker(), "compose", *_compose_files()]


def _tail(text: str, limit: int = 120) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:limit]


def _exec(service: str, command: str, timeout: int = 60, index: int = 1) -> str:
    result = _run(
        [*_compose_prefix(), "exec", "-T", "--index", str(index), service, "sh", "-c", command],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise CheckError(_tail(result.stderr or result.stdout))
    return result.stdout


def _parse_ps(output: str) -> list[dict[str, object]]:
    """Accept both a JSON array and one JSON object per line."""
    text = output.strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        return [parsed]

    services: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            item = json.loads(line.strip() or "null")
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            services.append(item)
    return services


def evaluate_services(
    services: list[dict[str, object]],
    required: tuple[str, ...],
) -> tuple[str, str]:
    """Every container of a required service must be running and, if it has a
    healthcheck, healthy. "starting" is a warning; "unhealthy" fails."""
    containers: dict[str, list[tuple[str, str]]] = {}
    for service in services:
        name = str(service.get("Service", "?"))
        state = str(service.get("State", "?")).lower()
        health = str(service.get("Health", "") or "").lower()
        containers.setdefault(name, []).append((state, health))

    def describe(name: str) -> str:
        entries = containers.get(name, [])
        labels = [health or state for state, health in entries]
        if len(set(labels)) == 1:
            label = labels[0]
            return f"{name}={label}" if len(labels) == 1 else f"{name}={len(labels)}x{label}"
        return f"{name}={'/'.join(labels)}"

    detail = ", ".join(describe(name) for name in sorted(containers))

    missing = [name for name in required if name not in containers]
    if missing:
        raise CheckError(f"missing services: {', '.join(missing)}")

    down = [
        name for name in required if any(state != "running" for state, _health in containers[name])
    ]
    if down:
        raise CheckError(f"not running: {', '.join(describe(name) for name in down)}")

    unhealthy = [
        name
        for name in required
        if any(health == "unhealthy" for _state, health in containers[name])
    ]
    if unhealthy:
        raise CheckError(f"unhealthy: {', '.join(describe(name) for name in unhealthy)}")

    starting = [
        name
        for name in required
        if any(health == "starting" for _state, health in containers[name])
    ]
    if starting:
        return WARN, f"still starting: {', '.join(describe(name) for name in starting)}"
    return PASS, detail


def replica_count(services: list[dict[str, object]], name: str) -> int:
    return sum(1 for service in services if str(service.get("Service")) == name)


def _compose_services() -> list[dict[str, object]]:
    result = _run([*_compose_prefix(), "ps", "--all", "--format", "json"])
    if result.returncode != 0:
        raise CheckError(_tail(result.stderr or result.stdout))
    services = _parse_ps(result.stdout)
    if not services:
        raise CheckError("no services reported by docker compose ps")
    return services


def check_compose(required: tuple[str, ...]) -> tuple[str, str]:
    return evaluate_services(_compose_services(), required)


def check_login_page(base_url: str, options: TlsOptions) -> tuple[str, str]:
    status, body = _get(f"{base_url.rstrip('/')}/login", options)
    if status != 200:
        raise CheckError(f"GET /login returned {status}: {_tail(body, 80)}")
    lowered = body.lower()
    if "password" not in lowered and "<form" not in lowered:
        raise CheckError("GET /login returned 200 but no login form")
    return PASS, "login page served"


def check_unauthenticated_redirect(
    base_url: str,
    options: TlsOptions,
) -> tuple[str, str]:
    status, body = _get(f"{base_url.rstrip('/')}/jobs", options, follow=False)
    if status in (301, 302, 303, 307, 308):
        return PASS, f"redirected with {status}"
    if status == 200 and "password" in body.lower():
        return PASS, "rendered login page"
    raise CheckError(f"GET /jobs returned {status}: {_tail(body, 80)}")


def _missing_tools(service: str, tools: tuple[str, ...], index: int) -> set[str]:
    names = " ".join(tools)
    output = _exec(
        service,
        f'for tool in {names}; do command -v "$tool" >/dev/null || echo "MISSING:$tool"; done',
        index=index,
    )
    return {
        line.split(":", 1)[1].strip()
        for line in output.splitlines()
        if line.strip().startswith("MISSING:")
    }


def _replicas(service: str) -> int:
    return max(replica_count(_compose_services(), service), 1)


def check_worker_tools(worker: str) -> tuple[str, str]:
    replicas = _replicas(worker)
    optional_missing: set[str] = set()
    for index in range(1, replicas + 1):
        missing = _missing_tools(worker, ("debmirror", "gpgv", "patch", "ed"), index)
        for tool in ("debmirror", "gpgv"):
            if tool in missing:
                raise CheckError(f"{tool} not installed in {worker} #{index}")
        optional_missing |= {tool for tool in ("patch", "ed") if tool in missing}

    scope = f"{replicas} {worker} containers" if replicas > 1 else worker
    if optional_missing:
        tools = " and ".join(sorted(optional_missing))
        return WARN, f"missing {tools} in {scope}; diffs disabled"
    return PASS, f"debmirror, gpgv, patch, ed present in {scope}"


def check_keyring(worker: str, path: str = "/gnupg/trustedkeys.gpg") -> tuple[str, str]:
    replicas = _replicas(worker)
    counts: list[int] = []
    for index in range(1, replicas + 1):
        output = _exec(
            worker,
            f"test -s {path} && "
            f"gpg --no-default-keyring --keyring {path} --list-keys 2>/dev/null | "
            f"grep -c '^pub'",
            index=index,
        )
        try:
            counts.append(int(output.strip().splitlines()[-1]))
        except (IndexError, ValueError) as error:
            raise CheckError(f"could not inspect {path} in {worker} #{index}") from error
        if counts[-1] == 0:
            raise CheckError(f"{path} contains no public keys in {worker} #{index}")
    return PASS, f"{counts[0]} keys in {path}" + (
        f" ({replicas} containers)" if replicas > 1 else ""
    )


def check_export_tools(export_worker: str) -> tuple[str, str]:
    missing = _missing_tools(export_worker, ("zstd", "gpg"), 1)
    if missing:
        raise CheckError(f"missing in {export_worker}: {', '.join(sorted(missing))}")
    return PASS, f"zstd, gpg present in {export_worker}"


def check_export_writable(export_worker: str, path: str) -> tuple[str, str]:
    # A top-level dot file: drop-folder housekeeping only looks at directories.
    probe = f"{path.rstrip('/')}/.mirrorhub-smoke-probe"
    _exec(
        export_worker,
        f'p="{probe}.$$"; echo smoke > "$p" || exit 1; rm -f "$p" || exit 1',
    )
    output = _exec(export_worker, f"test -w {path} && df -Pk {path} | tail -1")
    fields = output.split()
    free = f", {100.0 - float(fields[4].rstrip('%')):.1f}% free" if len(fields) >= 6 else ""
    return PASS, f"{path} writable{free}"


def check_data_space(
    worker: str,
    path: str,
    minimum_free_pct: float,
) -> tuple[str, str]:
    output = _exec(worker, f"df -Pk {path} | tail -1")
    fields = output.split()
    if len(fields) < 6:
        raise CheckError(f"unexpected df output for {path}")
    try:
        used_pct = float(fields[4].rstrip("%"))
    except ValueError as error:
        raise CheckError(f"unexpected df percentage: {fields[4]}") from error
    free_pct = 100.0 - used_pct
    if free_pct < minimum_free_pct:
        raise CheckError(f"{path} only {free_pct:.1f}% free")
    return PASS, f"{path} {free_pct:.1f}% free"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL", "https://localhost:8443"),
    )
    parser.add_argument(
        "--worker",
        default=os.environ.get("SMOKE_WORKER", "worker"),
    )
    parser.add_argument(
        "--required-services",
        default=os.environ.get("SMOKE_SERVICES", "web,worker,worker-export,proxy"),
    )
    parser.add_argument(
        "--data-path",
        default=os.environ.get("SMOKE_DATA_PATH", "/data"),
    )
    parser.add_argument(
        "--export-worker",
        default=os.environ.get("SMOKE_EXPORT_WORKER", "worker-export"),
    )
    parser.add_argument(
        "--export-path",
        default=os.environ.get("SMOKE_EXPORT_PATH", "/export"),
    )
    parser.add_argument("--min-free-pct", type=float, default=10.0)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification",
    )
    parser.add_argument("--ca-bundle", default=os.environ.get("SMOKE_CA_BUNDLE"))
    parser.add_argument(
        "--host-header",
        default=os.environ.get("SMOKE_HOST_HEADER"),
        help="override the Host header for virtual-host routing",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    options = TlsOptions(
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
        host_header=args.host_header,
    )
    required = tuple(item.strip() for item in args.required_services.split(",") if item.strip())

    plan: list[tuple[str, Callable[[], tuple[str, str]]]] = [
        ("compose services", lambda: check_compose(required)),
        ("login page", lambda: check_login_page(args.base_url, options)),
        (
            "auth redirect",
            lambda: check_unauthenticated_redirect(args.base_url, options),
        ),
        ("worker tools", lambda: check_worker_tools(args.worker)),
        ("gpg keyring", lambda: check_keyring(args.worker)),
        ("export tools", lambda: check_export_tools(args.export_worker)),
        (
            "export writable",
            lambda: check_export_writable(args.export_worker, args.export_path),
        ),
        (
            "mirror data space",
            lambda: check_data_space(
                args.worker,
                args.data_path,
                args.min_free_pct,
            ),
        ),
    ]
    results: list[Result] = []
    for label, check in plan:
        try:
            status, detail = check()
        except CheckError as error:
            results.append(Result(label, FAIL, str(error)))
        except Exception as error:
            results.append(Result(label, FAIL, f"{type(error).__name__}: {error}"))
        else:
            results.append(Result(label, status, detail))

    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2))
    else:
        width = max(len(result.name) for result in results)
        for result in results:
            print(f"{result.status:<5} {result.name:<{width}}  {result.detail}")

    failures = sum(1 for result in results if result.status == FAIL)
    if not args.json:
        print(f"\n{len(results) - failures}/{len(results)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
