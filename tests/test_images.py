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
