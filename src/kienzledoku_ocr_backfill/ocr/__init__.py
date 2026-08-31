"""Replaceable OCR backends."""

from .base import OcrBackend
from .command import CommandOcrBackend
from .ocrmypdf import OcrmypdfBackend

__all__ = ["CommandOcrBackend", "OcrBackend", "OcrmypdfBackend"]
