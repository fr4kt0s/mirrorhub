"""The web container trusts X-Forwarded-* only from the proxy network."""

from pathlib import Path

import yaml


def test_web_container_does_not_trust_every_proxy() -> None:
    root = Path(__file__).resolve().parents[1]
    assert '"--forwarded-allow-ips", "*"' not in (root / "Dockerfile").read_text()

    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    web = compose["services"]["web"]
    allowed = web["environment"]["FORWARDED_ALLOW_IPS"]
    assert "*" not in allowed
    assert web["networks"] == ["frontend"]
    assert compose["services"]["proxy"]["networks"] == ["frontend"]
    subnet = compose["networks"]["frontend"]["ipam"]["config"][0]["subnet"]
    assert allowed == subnet
