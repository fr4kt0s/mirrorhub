"""Importer error types. Each carries a stable process exit code."""

from __future__ import annotations


class ImporterError(Exception):
    """Base class; the default exit code covers I/O and resume problems."""

    exit_code = 9


class ConfigError(ImporterError):
    """Invalid configuration or command line (exit code 2)."""

    exit_code = 2


class VerificationFailed(ImporterError):
    """The export failed its checksum or format checks (exit code 3)."""

    exit_code = 3


class SignatureError(ImporterError):
    """The detached signature is missing or invalid (exit code 4)."""

    exit_code = 4


class ChainError(ImporterError):
    """The export does not follow the receiver's state (exit code 5)."""

    exit_code = 5


class UnsafeArchiveError(ImporterError):
    """The archive contains an unsafe member (exit code 6)."""

    exit_code = 6


class LockError(ImporterError):
    """Another importer holds the repository lock (exit code 7)."""

    exit_code = 7


class ToolMissingError(ImporterError):
    """A required external tool (zstd, gpgv) is missing (exit code 8)."""

    exit_code = 8


class IoFailure(ImporterError):
    """An I/O or permission problem, or an interrupted import (exit code 9)."""

    exit_code = 9


class PostCheckError(ImporterError):
    """The tree does not match files.json after applying (exit code 10)."""

    exit_code = 10


class DriftError(ImporterError):
    """The mirror tree differs from the inventory of the last applied export."""

    exit_code = 11
