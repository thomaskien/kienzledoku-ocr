"""Strictly serial orchestration; one document failure never stops the batch."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .handlers.document_reference import DocumentReferenceHandler
from .journal import Journal
from .models import InventoryItem


@dataclass(frozen=True)
class RunSummary:
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class BackfillProcessor:
    def __init__(
        self,
        handler: DocumentReferenceHandler,
        journal: Journal,
    ) -> None:
        self._handler = handler
        self._journal = journal

    def run(
        self,
        inventory: Iterable[InventoryItem],
        *,
        apply: bool,
        resume: bool,
    ) -> RunSummary:
        completed = self._journal.completed_object_ids() if resume else set()
        counts: Counter[str] = Counter()
        for item in inventory:
            if item.object_id in completed:
                self._journal.write(
                    {
                        "version": "1.00",
                        "patientNumber": item.patient_number,
                        "objectId": item.object_id,
                        "classId": item.class_id,
                        "filename": item.filename,
                        "mimeType": item.mime_type,
                        "cdnVerweis": item.cdn_reference,
                        "status": "resume_skipped",
                    }
                )
                counts["resume_skipped"] += 1
                continue
            try:
                status = self._handler.process(item, apply=apply)
            except Exception as exc:
                # Last-resort guard: even an implementation error is per-document.
                self._journal.write(
                    {
                        "version": "1.00",
                        "patientNumber": item.patient_number,
                        "objectId": item.object_id,
                        "classId": item.class_id,
                        "filename": item.filename,
                        "mimeType": item.mime_type,
                        "cdnVerweis": item.cdn_reference,
                        "status": "internal_error",
                        "errorType": type(exc).__name__,
                        "error": str(exc)[:2000],
                    }
                )
                status = "internal_error"
            counts[status] += 1
        return RunSummary(dict(counts))
