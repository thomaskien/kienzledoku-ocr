"""Domain-specific errors used to classify each document independently."""

from __future__ import annotations

from typing import Optional


class BackfillError(RuntimeError):
    """Base class for expected backfill failures."""


class DatabaseReadError(BackfillError):
    pass


class HttpRequestError(BackfillError):
    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class MissingCdnError(HttpRequestError):
    pass


class ApsFindError(BackfillError):
    pass


class ApsUpdateError(BackfillError):
    pass


class OcrError(BackfillError):
    pass


class VerificationError(BackfillError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))
