"""OCR backend contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class OcrBackend(ABC):
    @abstractmethod
    def extract_text(self, path: Path, mime_type: str) -> str:
        """Extract all text without silently shortening it."""
        raise NotImplementedError
