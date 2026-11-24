"""wget command of tree mirrors: generated options, display, parsing and the allowlist."""

import shlex

import pytest

from mirrorhub import wgetcmd

URL = "https://download.docker.com/linux/ubuntu/"
TARGET = "/data/docker-tree"


def _generated() -> list[str]:
    return wgetcmd.generated_args(
        upstream_root="/linux/ubuntu",
        exclude_regex="/(test|nightly)/",
        keep_arches=["amd64"],
        bwlimit_kbps=500,
    )


def test_generated_options_and_display() -> None:
    args = _generated()
    assert args[:5] == [
        "--recursive",
        "--level=inf",
        "--no-parent",
        "--no-host-directories",
        "--cut-dirs=2",
    ]
    assert "--limit-rate=500k" in args
    line = wgetcmd.display(args, target=TARGET, url=URL)
    tokens = shlex.split(line)
    assert tokens[0] == "wget"
    assert tokens[-3:] == [f"--directory-prefix={TARGET}", "--timestamping", URL]
    assert wgetcmd.validate_args(args) is None


def test_displayed_command_parses_back_to_the_same_options() -> None:
    args = _generated()
    line = wgetcmd.display(args, target=TARGET, url=URL)
    assert wgetcmd.parse_command(line, url=URL, target=TARGET) == args


def test_parse_normalises_separate_values_and_line_continuations() -> None:
    text = f"wget --recursive \\\n  --level inf \\\n  --accept-regex '\\.deb$' --wait 2 {URL}"
    assert wgetcmd.parse_command(text, url=URL, target=TARGET) == [
        "--recursive",
        "--level=inf",
        "--accept-regex=\\.deb$",
        "--wait=2",
    ]


def test_url_may_be_left_out() -> None:
    assert wgetcmd.parse_command("wget --recursive", url=URL, target=TARGET) == ["--recursive"]


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("curl --recursive", "must start with 'wget'"),
        ("wget -r", "use the long form"),
        ("wget --use-askpass=/bin/sh", "--use-askpass is not allowed"),
        ("wget --execute=robots=off", "--execute is not allowed"),
        ("wget --output-document=/etc/passwd", "--output-document is not allowed"),
        ("wget --post-file=/etc/shadow", "--post-file is not allowed"),
        ("wget --span-hosts", "--span-hosts is not allowed"),
        ("wget --input-file=/tmp/x", "--input-file is not allowed"),
        ("wget --directory-prefix=/etc", "--directory-prefix is set by MirrorHub"),
        ("wget --spider", "--spider is added by MirrorHub"),
        ("wget --tries=999", "--tries: integer 1-50"),
        ("wget --level=deep", "--level: 'inf' or an integer"),
        ("wget --limit-rate=fast", "--limit-rate: a number"),
        ("wget --regex-type=glob", "--regex-type: one of pcre, posix"),
        ("wget --recursive=yes", "--recursive takes no value"),
        ("wget --wait", "--wait requires a value"),
        ("wget https://evil.example/", "the only URL allowed is"),
        ("wget 'unterminated", "No closing quotation"),
    ],
)
def test_rejected_commands(command: str, message: str) -> None:
    with pytest.raises(wgetcmd.CommandError) as excinfo:
        wgetcmd.parse_command(command, url=URL, target=TARGET)
    assert any(message in text for text in excinfo.value.messages), excinfo.value.messages


def test_control_characters_in_values_are_rejected() -> None:
    with pytest.raises(wgetcmd.CommandError):
        wgetcmd.parse_command("wget '--accept-regex=a\nb'", url=URL, target=TARGET)


def test_all_errors_are_reported_together() -> None:
    with pytest.raises(wgetcmd.CommandError) as excinfo:
        wgetcmd.parse_command("wget -r --use-askpass=x --tries=0", url=URL, target=TARGET)
    assert len(excinfo.value.messages) == 3


def test_validate_stored_args() -> None:
    assert wgetcmd.validate_args(["--recursive", "--wait=2"]) is None
    assert "not allowed" in (wgetcmd.validate_args(["--execute=x"]) or "")
    assert "malformed" in (wgetcmd.validate_args(["-r"]) or "")
    assert "at most" in (wgetcmd.validate_args(["--recursive"] * 61) or "")
