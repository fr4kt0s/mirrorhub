"""The web container trusts X-Forwarded-* only from the Caddy container."""

import ipaddress
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RE = re.compile(r"^\$\{[A-Z_]+:-([^}]+)\}$")


def _default(value: Any) -> str:
    """Resolve a Compose ${VAR:-default} reference to its default."""
    match = DEFAULT_RE.fullmatch(str(value))
    assert match, f"expected ${{VAR:-default}}, got {value!r}"
    return match.group(1)


def test_web_trusts_only_the_proxy_address() -> None:
    assert '"--forwarded-allow-ips", "*"' not in (ROOT / "Dockerfile").read_text()

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    web = compose["services"]["web"]
    proxy = compose["services"]["proxy"]
    ipam = compose["networks"]["frontend"]["ipam"]["config"][0]

    allowed = ipaddress.ip_address(_default(web["environment"]["FORWARDED_ALLOW_IPS"]))
    proxy_ip = ipaddress.ip_address(_default(proxy["networks"]["frontend"]["ipv4_address"]))
    subnet = ipaddress.ip_network(_default(ipam["subnet"]))
    dynamic = ipaddress.ip_network(_default(ipam["ip_range"]))

    assert web["networks"] == ["frontend"]
    assert allowed == proxy_ip
    assert proxy_ip in subnet
    assert proxy_ip not in dynamic, "a dynamically addressed container could take it"
    assert proxy_ip != next(subnet.hosts()), "the gateway is the Docker host"
    assert dynamic.subnet_of(subnet)  # type: ignore[arg-type]


def test_env_example_matches_compose_defaults() -> None:
    env = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    compose = (ROOT / "docker-compose.yml").read_text()
    for key in ("MIRRORHUB_FRONTEND_SUBNET", "MIRRORHUB_FRONTEND_IP_RANGE", "MIRRORHUB_PROXY_IP"):
        assert f"${{{key}:-{env[key]}}}" in compose, key


def test_caddy_uses_configured_hostname_and_skips_trust_install() -> None:
    caddyfile = (ROOT / "caddy" / "Caddyfile").read_text()
    assert "skip_install_trust" in caddyfile
    assert "https://{$MIRRORHUB_HOSTNAME:localhost}:8443" in caddyfile
