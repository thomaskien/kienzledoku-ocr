"""Conflict-safe rollback based only on verified `updated` journal records."""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any, Callable, Optional

from . import __version__
from .aps_client import ApsClient
from .db_reader import DatabaseReader
from .errors import ApsFindError, ApsUpdateError, DatabaseReadError
from .journal import Journal
from .models import DocumentSnapshot
from .processor import RunSummary


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_invariants(before: DocumentSnapshot, after: DocumentSnapshot) -> bool:
    if before.object_id != after.object_id:
        return False
    return all(
        before.dto.get(key) == after.dto.get(key)
        for key in ("verweis", "gueltigkeitszeitpunkt", "fachinformationstyp")
    )


class RollbackProcessor:
    def __init__(
        self,
        database: DatabaseReader,
        aps: ApsClient,
        journal: Journal,
        *,
        report: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._database = database
        self._aps = aps
        self._journal = journal
        self._report = report or (lambda message: None)

    def _write(self, source: dict[str, Any], status: str, **extra: Any) -> None:
        record = {
            "version": __version__,
            "patientNumber": source.get("patientNumber"),
            "objectId": source.get("objectId"),
            "classId": source.get("classId"),
            "filename": source.get("filename"),
            "mimeType": source.get("mimeType"),
            "cdnVerweis": source.get("cdnVerweis"),
            "status": status,
            **extra,
        }
        self._journal.write(record)
        self._report(f"{record['patientNumber']} / {record['objectId']}: {status}")

    def _read_current(self, object_id: str) -> DocumentSnapshot:
        revision = self._database.current_revision(object_id)
        return self._aps.find(object_id, revision)

    def run(
        self,
        *,
        apply: bool,
        patient_number: Optional[str] = None,
        object_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> RunSummary:
        candidates = self._journal.rollback_candidates()
        filtered = [
            record
            for record in candidates
            if (patient_number is None or str(record.get("patientNumber")) == patient_number)
            and (object_id is None or str(record.get("objectId")) == object_id)
        ]
        if limit is not None:
            filtered = filtered[:limit]

        counts: Counter[str] = Counter()
        for source in filtered:
            current: Optional[DocumentSnapshot] = None
            try:
                current = self._read_current(str(source["objectId"]))
                expected_revision = source.get("revisionAfter")
                expected_hash = source.get("newTextSha256")
                old_text = source.get("oldText")
                if (
                    not isinstance(expected_revision, int)
                    or current.revision != expected_revision
                    or not isinstance(expected_hash, str)
                    or _sha256_text(current.text) != expected_hash
                    or not isinstance(old_text, str)
                ):
                    self._write(
                        source,
                        "rollback_conflict",
                        revisionBefore=current.revision,
                        expectedRevision=expected_revision,
                        currentTextSha256=_sha256_text(current.text),
                        expectedTextSha256=expected_hash,
                    )
                    counts["rollback_conflict"] += 1
                    continue

                if not apply:
                    self._write(
                        source,
                        "rollback_dry_run",
                        revisionBefore=current.revision,
                        currentTextSha256=_sha256_text(current.text),
                        restoredTextSha256=_sha256_text(old_text),
                    )
                    counts["rollback_dry_run"] += 1
                    continue

                self._write(
                    source,
                    "rollback_prepared",
                    revisionBefore=current.revision,
                    currentTextSha256=_sha256_text(current.text),
                    restoredTextSha256=_sha256_text(old_text),
                )
                self._aps.update_text(current, old_text)
                after = self._read_current(str(source["objectId"]))
                if (
                    after.text != old_text
                    or after.revision <= current.revision
                    or not _same_invariants(current, after)
                ):
                    raise RuntimeError("Rollback-Verifikation fehlgeschlagen")
                self._write(
                    source,
                    "rolled_back",
                    revisionBefore=current.revision,
                    revisionAfter=after.revision,
                    restoredTextSha256=_sha256_text(old_text),
                )
                counts["rolled_back"] += 1
            except (DatabaseReadError, ApsFindError, ApsUpdateError, RuntimeError) as exc:
                self._write(
                    source,
                    "rollback_failed",
                    revisionBefore=current.revision if current else None,
                    errorType=type(exc).__name__,
                    error=str(exc)[:2000],
                )
                counts["rollback_failed"] += 1
            except Exception as exc:
                self._write(
                    source,
                    "rollback_failed",
                    revisionBefore=current.revision if current else None,
                    errorType=type(exc).__name__,
                    error=str(exc)[:2000],
                )
                counts["rollback_failed"] += 1
        return RunSummary(dict(counts))
