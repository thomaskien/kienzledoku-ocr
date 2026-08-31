"""Deliberately disabled until classid 59 has a verified update endpoint."""

from __future__ import annotations

from ..models import InventoryItem


class ImageEntryHandler:
    def supports(self, item: InventoryItem) -> bool:
        return False
