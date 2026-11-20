"""Container image contents that the sync tools rely on."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_debmirror_images_install_patch_and_ed() -> None:
    """debmirror needs patch and ed to apply index diffs (pdiffs)."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    for stage in ("worker", "worker-debmirror"):
        start = dockerfile.index(f" AS {stage}\n")
        end = dockerfile.find("\nFROM ", start)
        block = dockerfile[start : end if end != -1 else None]
        assert " patch " in block and " ed " in block, stage


def test_host_bind_volumes_are_mounted_with_nocopy() -> None:
    """Docker would otherwise copy the image directory into an empty host directory
    on first start and reset its owner and mode to root:root 0755."""
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    seen = 0
    for name, service in compose["services"].items():
        for mount in service.get("volumes", []):
            source = mount.get("source") if isinstance(mount, dict) else mount.split(":")[0]
            if source not in ("mirror_data", "export_dir"):
                continue
            seen += 1
            assert isinstance(mount, dict), f"{name}: use the long syntax with nocopy"
            assert mount.get("volume", {}).get("nocopy") is True, f"{name}: {source}"
    assert seen == 5
