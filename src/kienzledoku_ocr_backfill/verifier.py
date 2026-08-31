"""Strict post-update verification."""

from __future__ import annotations

from typing import Any

from .errors import VerificationError
from .models import DocumentSnapshot


def _value(dto: dict[str, Any], key: str) -> Any:
    return dto.get(key)


def verify_update(
    before: DocumentSnapshot,
    after: DocumentSnapshot,
    expected_text: str,
    footer: str,
) -> None:
    problems: list[str] = []

    if after.text != expected_text:
        problems.append("gespeicherter Text stimmt nicht exakt mit dem Update überein")
    if not after.text.endswith(footer):
        problems.append("Footer fehlt oder steht nicht exakt am Textende")
    if not after.text.startswith(before.text.rstrip()):
        problems.append("vorheriger Text ist nicht als Präfix erhalten")
    if before.object_id != after.object_id:
        problems.append("objectId wurde verändert")
    if after.revision <= before.revision:
        problems.append("Revision wurde nach dem Update nicht erhöht")

    for key in ("verweis", "gueltigkeitszeitpunkt", "fachinformationstyp"):
        if _value(before.dto, key) != _value(after.dto, key):
            problems.append(f"{key} wurde verändert")

    if problems:
        raise VerificationError(problems)
