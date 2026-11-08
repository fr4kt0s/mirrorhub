"""Importer error types. Each carries a stable process exit code."""

from __future__ import annotations


class ImporterError(Exception):
    """Base class; the default exit code covers I/O and resume problems."""

    exit_code = 9


class ConfigError(ImporterError):
    exit_code = 2


class VerificationFailed(ImporterError):
    exit_code = 3


class SignatureError(ImporterError):
    exit_code = 4


class ChainError(ImporterError):
    exit_code = 5


class UnsafeArchiveError(ImporterError):
    exit_code = 6


class LockError(ImporterError):
    exit_code = 7


class ToolMissingError(ImporterError):
    exit_code = 8


class IoFailure(ImporterError):
    exit_code = 9


class PostCheckError(ImporterError):
    exit_code = 10
