"""Strict post-update verification."""

from __future__ import annotations

from typing import Any, Optional

from .errors import VerificationError
from .formatter import BLOCK_END
from .models import DocumentSnapshot


def _value(dto: dict[str, Any], key: str) -> Any:
    return dto.get(key)


def verify_update(
    before: DocumentSnapshot,
    after: DocumentSnapshot,
    expected_text: str,
    footer: str,
    preserved_prefix: Optional[str] = None,
) -> None:
    problems: list[str] = []

    if after.text != expected_text:
        problems.append("gespeicherter Text stimmt nicht exakt mit dem Update überein")
    if not after.text.endswith(f"\n{footer}\n{BLOCK_END}"):
        problems.append("Footer oder OCR-Endmarke fehlt am Textende")
    prefix = before.text if preserved_prefix is None else preserved_prefix
    if not after.text.startswith(prefix.rstrip()):
        problems.append("zu erhaltender Basistext ist nicht als Präfix erhalten")
    if before.object_id != after.object_id:
        problems.append("objectId wurde verändert")
    if after.revision <= before.revision:
        problems.append("Revision wurde nach dem Update nicht erhöht")

    for key in ("verweis", "gueltigkeitszeitpunkt", "fachinformationstyp"):
        if _value(before.dto, key) != _value(after.dto, key):
            problems.append(f"{key} wurde verändert")

    if problems:
        raise VerificationError(problems)
