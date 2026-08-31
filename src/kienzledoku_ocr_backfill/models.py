"""Data objects shared by inventory, processing and journaling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class InventoryItem:
    patient_number: str
    object_id: str
    revision: int
    class_id: int
    valid_at: Optional[str]
    cdn_reference: str
    filename: Optional[str]
    mime_type: Optional[str]
    size: Optional[int]


@dataclass(frozen=True)
class DocumentSnapshot:
    dto: dict[str, Any]
    object_id: str
    revision: int
    text: str
