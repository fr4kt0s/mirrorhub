"""Keep only selected CPU architectures in a wget directory-tree mirror.

wget can only reject URLs by regex, and POSIX regex has no "everything except". So the
operator names the architectures to keep and this module builds the reject regex for
all other known architectures.

An architecture counts as present in a URL when one of its names stands between
separators (``/``, ``_``, ``.``, ``-``) or at the end, which covers the usual layouts:
``binary-arm64/``, ``pool/stable/arm64/``, ``foo_1.0_arm64.deb``, ``foo-1.0.aarch64.rpm``,
``tool-linux-arm64.tar.gz``. Architecture-independent names (``all``, ``noarch``,
``any``) and ambiguous short names (``arm``, ``x86``, ``x64``, ``386``) are never
rejected.
"""

from __future__ import annotations

# Canonical name (Debian style) -> all names used for it in common repository layouts.
ARCH_GROUPS: dict[str, tuple[str, ...]] = {
    "amd64": ("amd64", "x86_64"),
    "i386": ("i386", "i486", "i586", "i686"),
    "arm64": ("arm64", "aarch64"),
    "armhf": ("armhf", "armhfp", "armv7", "armv7l", "armv7hl"),
    "armel": ("armel", "armv5", "armv5tel", "armv6", "armv6l"),
    "ppc64el": ("ppc64el", "ppc64le"),
    "ppc64": ("ppc64",),
    "s390x": ("s390x",),
    "riscv64": ("riscv64",),
    "mips64el": ("mips64el",),
    "mipsel": ("mipsel",),
    "loong64": ("loong64", "loongarch64"),
}

_ALIASES: dict[str, str] = {
    name: canonical for canonical, names in ARCH_GROUPS.items() for name in names
}

SEPARATORS = "[/_.-]"


def canonical(name: str) -> str | None:
    """Canonical architecture for a name or alias, or None if unknown."""
    return _ALIASES.get(name.strip().lower())


def validate_keep(names: list[str]) -> str | None:
    """Error message for unknown names, or None."""
    if len(names) > len(_ALIASES):
        return "architectures to keep: too many entries"
    unknown = [name for name in names if canonical(name) is None]
    if unknown:
        return (
            f"architectures to keep: unknown {', '.join(repr(n[:20]) for n in unknown[:5])}; "
            f"use {', '.join(ARCH_GROUPS)}"
        )
    kept = {canonical(name) for name in names}
    if names and kept >= set(ARCH_GROUPS):
        return "architectures to keep: all known architectures selected; leave the field empty"
    return None


def reject_regex(keep: list[str]) -> str:
    """POSIX ERE matching URLs of every known architecture not in ``keep``; "" keeps all."""
    kept = {canonical(name) for name in keep} - {None}
    if not kept:
        return ""
    rejected = [name for group, names in ARCH_GROUPS.items() if group not in kept for name in names]
    # Longest first, so "ppc64el" is tried before "ppc64" (both are separator-bounded anyway).
    rejected.sort(key=lambda name: (-len(name), name))
    return f"{SEPARATORS}({'|'.join(rejected)})({SEPARATORS}|$)"


def combine_reject(user_regex: str, keep: list[str]) -> str:
    """The operator's exclude regex and the architecture filter as one --reject-regex."""
    generated = reject_regex(keep)
    if user_regex and generated:
        return f"({user_regex})|({generated})"
    return user_regex or generated
