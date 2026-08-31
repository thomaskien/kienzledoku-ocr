"""Strictly serial orchestration; one document failure never stops the batch."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from . import __version__
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
        *,
        report: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._handler = handler
        self._journal = journal
        self._report = report or (lambda message: None)

    def run(
        self,
        inventory: Iterable[InventoryItem],
        *,
        apply: bool,
        resume: bool,
    ) -> RunSummary:
        items = list(inventory)
        completed = self._journal.completed_object_ids() if resume else set()
        counts: Counter[str] = Counter()
        for index, item in enumerate(items):
            if item.object_id in completed:
                self._report("")
                self._report("Identifiziere Dokument")
                self._report(f"Dokumenten-ID: {item.object_id}")
                self._report(f"Dokumentendatum: {item.valid_at or '(nicht verfügbar)'}")
                patient = item.patient_number
                if item.patient_name:
                    patient += f" {item.patient_name}"
                self._report(f"Patient: {patient}")
                self._report("Status: resume_skipped")
                self._journal.write(
                    {
                        "version": __version__,
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
            else:
                try:
                    status = self._handler.process(item, apply=apply)
                except Exception as exc:
                    # Last-resort guard: even an implementation error is per-document.
                    self._journal.write(
                        {
                            "version": __version__,
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
                    self._report("Status: internal_error")
                    self._report(f"Fehler: {str(exc)[:500]}")
                    status = "internal_error"
                counts[status] += 1
            self._report("Dokument fertig")
            if index + 1 < len(items):
                self._report("Nächstes Dokument")
        return RunSummary(dict(counts))
