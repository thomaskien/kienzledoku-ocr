"""Idempotence marker and exact T2med text composition."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo


MARKER_RE = re.compile(r"(?m)^kienzledoku OCR v[0-9.]+,\s")
BERLIN = ZoneInfo("Europe/Berlin")


def contains_ocr_marker(text: Optional[str]) -> bool:
    return bool(MARKER_RE.search(text or ""))


def make_footer(version: str, when: Optional[datetime] = None) -> str:
    current = when or datetime.now(BERLIN)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BERLIN)
    else:
        current = current.astimezone(BERLIN)
    return f"kienzledoku OCR v{version}, {current:%d.%m.%Y %H:%M}"


def compose_text(old_text: Optional[str], ocr_text: str, footer: str) -> str:
    """Compose exactly as specified: two empty lines before OCR."""
    return (
        (old_text or "").rstrip()
        + "\n\n\n"
        + ocr_text.strip()
        + "\n\n"
        + footer
    )
