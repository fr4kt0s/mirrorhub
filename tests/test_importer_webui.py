"""Importer web UI (`serve`): command building, overview, HTTP guards, runs, password."""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from mirrorhub.importer import webui
from mirrorhub.importer.cli import main
from mirrorhub.importer.config import Config, load_config
from test_importer import ID1, ID2, T1, T2, build_export


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Config, Path]:
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    return load_config(settings), settings


def _form(**fields: str) -> dict[str, list[str]]:
    return {key: [value] for key, value in fields.items()}


# ---- command lines -------------------------------------------------------------------


def test_build_command_for_every_cli_command(setup: tuple[Config, Path]) -> None:
    config, _ = setup
    export = config.export_dir / "ubuntu" / ID1
    export.mkdir(parents=True)
    cases = [
        (_form(command="status"), ["status"]),
        (_form(command="status", slug="ubuntu"), ["status", "--slug", "ubuntu"]),
        (_form(command="import", scope="all"), ["import", "--all"]),
        (
            _form(
                command="import",
                scope="slug",
                slug="ubuntu",
                dry_run="on",
                full_check="on",
                no_tree_check="on",
                verify_first="on",
                drift_output="/tmp/drift",
            ),
            [
                "import",
                "--slug",
                "ubuntu",
                "--dry-run",
                "--full-check",
                "--no-tree-check",
                "--verify-first",
                "--drift-output",
                "/tmp/drift",
            ],
        ),
        (
            _form(command="import", scope="export", export=str(export)),
            ["import", "--export", str(export)],
        ),
        (
            _form(
                command="import",
                scope="slug",
                slug="ubuntu",
                rebase="on",
                discard_staging="on",
                confirm="on",
            ),
            ["import", "--slug", "ubuntu", "--rebase", "--discard-staging"],
        ),
        (_form(command="check"), ["check", "--all"]),
        (
            _form(command="check", slug="ubuntu", hash="on", output_dir="/tmp/check"),
            ["check", "--slug", "ubuntu", "--hash", "--output-dir", "/tmp/check"],
        ),
        (_form(command="adopt", slug="ubuntu"), ["adopt", "--slug", "ubuntu", "--auto"]),
        (
            _form(command="adopt", slug="ubuntu", export_id=ID2, hash="on"),
            ["adopt", "--slug", "ubuntu", "--export-id", ID2, "--hash"],
        ),
    ]
    for form, expected in cases:
        assert webui.build_command(form, config)[1] == expected


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"command": "import", "scope": "slug", "slug": "../etc"}, "invalid repository slug"),
        ({"command": "import"}, "choose what to import"),
        ({"command": "import", "scope": "export", "export": "/etc"}, "below"),
        ({"command": "import", "scope": "export", "export": "relative"}, "absolute path"),
        ({"command": "import", "scope": "slug", "slug": "u", "rebase": "on"}, "confirmation"),
        ({"command": "import", "scope": "all", "rebase": "on", "confirm": "on"}, "all repos"),
        ({"command": "check", "output_dir": "out"}, "absolute path"),
        ({"command": "adopt", "slug": "u", "export_id": "latest"}, "invalid export id"),
        ({"command": "rm"}, "unknown command"),
    ],
)
def test_build_command_rejects(
    setup: tuple[Config, Path], fields: dict[str, str], message: str
) -> None:
    with pytest.raises(webui.FormError, match=message):
        webui.build_command(_form(**fields), setup[0])


def test_overview_lists_state_and_pending_exports(setup: tuple[Config, Path]) -> None:
    config, settings = setup
    build_export(config.export_dir, "20261101T000000Z", "full", T1)
    assert main(["--config", str(settings), "import", "--slug", "ubuntu"]) == 0
    build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    build_export(config.export_dir, "20261101T000000Z", "full", T1, slug="docker")

    rows = {row.slug: row for row in webui.overview(config)}
    assert rows["ubuntu"].last_export_id == ID1 and rows["ubuntu"].applied == 1
    assert rows["ubuntu"].pending == [ID2] and rows["ubuntu"].applied_at
    assert rows["docker"].last_export_id is None and rows["docker"].pending == [ID1]


def test_parse_listen() -> None:
    assert webui.parse_listen("127.0.0.1:8080") == ("127.0.0.1", 8080)
    assert webui.parse_listen("[::1]:9000") == ("::1", 9000)
    assert webui.parse_listen(":8080") == ("127.0.0.1", 8080)
    for bad in ("8080", "host:http", "host:70000"):
        with pytest.raises(ValueError):
            webui.parse_listen(bad)


# ---- HTTP ------------------------------------------------------------------------------


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""

    def request(
        self,
        method: str,
        path: str,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urlencode(fields or {}) if method == "POST" else None
        sent = {"Host": f"localhost:{self.port}"}
        if body is not None:
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie:
            sent["Cookie"] = self.cookie
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read().decode()
        result = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        if "set-cookie" in result:
            self.cookie = result["set-cookie"].split(";")[0]
        return response.status, result, data

    def csrf(self) -> str:
        page = self.request("GET", "/")[2]
        match = re.search(r"name='csrf' value='([0-9a-f]{64})'", page)
        assert match
        return match.group(1)

    def wait(self, run_id: int) -> dict[str, Any]:
        for _ in range(200):
            data: dict[str, Any] = json.loads(
                self.request("GET", f"/runs/{run_id}/output?offset=0")[2]
            )
            if not data["running"]:
                return data
            time.sleep(0.1)
        raise AssertionError("run did not finish")


@pytest.fixture
def server(setup: tuple[Config, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def start(password: str | None = None) -> tuple[webui.WebUI, Client]:
        config, settings = setup
        ui = webui.WebUI(config, settings, password)
        httpd = webui.make_server(ui, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return ui, Client(httpd.server_address[1])

    servers: list[Any] = []
    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_guards_host_origin_and_csrf(server: Any) -> None:
    _, client = server()
    assert client.request("GET", "/")[0] == 200
    assert client.request("GET", "/", headers={"Host": "evil.example"})[0] == 403
    assert client.request("GET", "/health", headers={"Host": "127.0.0.1"})[0] == 200
    assert client.request("POST", "/run", {"command": "status"})[0] == 403
    token = client.csrf()
    status = client.request(
        "POST",
        "/run",
        {"csrf": token, "command": "status"},
        {"Origin": "http://evil.example"},
    )[0]
    assert status == 403
    status, _, page = client.request("POST", "/run", {"csrf": token, "command": "x"})
    assert status == 400 and "unknown command" in page
    headers = client.request("GET", "/")[1]
    assert "script-src 'nonce-" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


def test_import_run_shows_output_and_exit_code(server: Any, setup: tuple[Config, Path]) -> None:
    config, _ = setup
    build_export(config.export_dir, "20261101T000000Z", "full", T1)
    _, client = server()
    token = client.csrf()

    status, headers, _ = client.request(
        "POST", "/run", {"csrf": token, "command": "import", "scope": "slug", "slug": "ubuntu"}
    )
    assert status == 303 and headers["location"] == "/runs/1"
    data = client.wait(1)
    assert f"applied {ID1}: 4 added" in data["text"]
    assert "# exit code 0 (ok)" in data["text"]
    assert "INFO    applied" in data["text"]  # readable log lines, not JSON

    page = client.request("GET", "/runs/1")[2]
    assert "--log-format text" in page and "exit code 0" in page
    overview = client.request("GET", "/")[2]
    assert ID1 in overview and "import ubuntu" in overview

    (config.mirror_root / "ubuntu" / "pool/a.deb").unlink()
    client.request("POST", "/run", {"csrf": token, "command": "check", "slug": "ubuntu"})
    data = client.wait(2)
    assert "missing: pool/a.deb" in data["text"] and "exit code 11" in data["text"]
    assert "state differs" in client.request("GET", "/runs/2")[2]


def test_one_run_at_a_time_and_cancel(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webui, "BOOT", "import time; print('working', flush=True); time.sleep(30)")
    ui, client = server()
    token = client.csrf()
    client.request("POST", "/run", {"csrf": token, "command": "status"})
    status, _, page = client.request("POST", "/run", {"csrf": token, "command": "status"})
    assert status == 400 and "another run is still active" in page

    assert client.request("POST", "/runs/1/cancel", {"csrf": token})[0] == 303
    data = client.wait(1)
    assert "cancel requested" in data["text"] and "(cancelled)" in data["text"]
    assert ui.runner.runs[1].exit_code == -15


def test_password_login(server: Any) -> None:
    _, client = server("s3cret")
    status, _, page = client.request("GET", "/")
    assert status == 200 and "type='password'" in page and "Repositories" not in page
    token = re.search(r"name='csrf' value='([0-9a-f]{64})'", page)
    assert token
    assert client.request("POST", "/run", {"csrf": token.group(1), "command": "status"})[0] == 303
    assert client.request("GET", "/runs/1")[0] == 200 and not client.cookie  # login page
    assert "Run #1" not in client.request("GET", "/runs/1")[2]
    status = client.request("POST", "/login", {"csrf": token.group(1), "password": "nope"})[0]
    assert status == 401
    status, headers, _ = client.request(
        "POST", "/login", {"csrf": token.group(1), "password": "s3cret"}
    )
    assert status == 303 and "HttpOnly" in headers["set-cookie"]
    assert "Repositories" in client.request("GET", "/")[2]


def test_serve_with_a_bad_listen_address_is_a_config_error(setup: tuple[Config, Path]) -> None:
    _, settings = setup
    assert main(["--config", str(settings), "serve", "--listen", "nowhere"]) == 2
