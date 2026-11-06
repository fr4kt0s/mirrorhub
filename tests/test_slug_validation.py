import pytest

from mirrorhub.security.validators import validate_slug


@pytest.mark.parametrize(
    "value",
    ["archive.ubuntu.com", "ubuntu", "mirror-1.example.org", "a", "1.2.3"],
)
def test_valid_ids(value: str) -> None:
    assert validate_slug(value) is None


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", ".x", "x.", "a..b", "-a", "a-", "a.-b", "a-.b",
     "A.b", "a_b", "a/b", "a" * 64 + ".com", "a." * 130 + "a"],
)
def test_invalid_ids(value: str) -> None:
    assert validate_slug(value) is not None