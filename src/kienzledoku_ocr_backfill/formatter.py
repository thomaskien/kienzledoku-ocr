"""Idempotence marker and exact T2med text composition."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo


BLOCK_BEGIN = "----- BEGINN kienzledoku OCR -----"
BLOCK_END = "----- ENDE kienzledoku OCR -----"
MARKER_RE = re.compile(
    rf"(?m)^(?:kienzledoku OCR v[0-9.]+,\s|{re.escape(BLOCK_BEGIN)}$)"
)
BLOCK_RE = re.compile(
    rf"(?s)(?:^|\n{{3}}){re.escape(BLOCK_BEGIN)}\n.*?\n\n"
    rf"kienzledoku OCR v[0-9.]+,\s[^\r\n]+\n{re.escape(BLOCK_END)}\s*\Z"
)
BERLIN = ZoneInfo("Europe/Berlin")
MARKER_DETAILS_RE = re.compile(
    r"(?m)^kienzledoku OCR v(?P<version>[0-9.]+),\s*(?P<when>[^\r\n]+)$"
)


def contains_ocr_marker(text: Optional[str]) -> bool:
    return bool(MARKER_RE.search(text or ""))


def latest_ocr_marker(text: Optional[str]) -> Optional[tuple[str, str]]:
    matches = list(MARKER_DETAILS_RE.finditer(text or ""))
    if not matches:
        return None
    match = matches[-1]
    return match.group("version"), match.group("when").strip()


def first_text_lines(text: Optional[str], *, count: int = 2) -> list[str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[:count] or ["(leer)"]


def remove_managed_ocr_block(text: Optional[str]) -> Optional[str]:
    """Return the preserved prefix only for a complete, terminal managed block."""
    value = text or ""
    match = BLOCK_RE.search(value)
    if match is None:
        return None
    return value[: match.start()].rstrip()


def make_footer(version: str, when: Optional[datetime] = None) -> str:
    current = when or datetime.now(BERLIN)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BERLIN)
    else:
        current = current.astimezone(BERLIN)
    return f"kienzledoku OCR v{version}, {current:%d.%m.%Y %H:%M}"


def compose_text(old_text: Optional[str], ocr_text: str, footer: str) -> str:
    """Append one complete managed OCR block after two empty lines."""
    return (
        (old_text or "").rstrip()
        + "\n\n\n"
        + BLOCK_BEGIN
        + "\n"
        + ocr_text.strip()
        + "\n\n"
        + footer
        + "\n"
        + BLOCK_END
    )
