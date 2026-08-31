"""Serial classid-60 PDF processing, including write-ahead journal entries."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from .. import __version__
from ..aps_client import ApsClient
from ..cdn_client import CdnClient
from ..db_reader import DatabaseReader, is_pdf_file_info
from ..errors import (
    ApsFindError,
    ApsUpdateError,
    DatabaseReadError,
    HttpRequestError,
    MissingCdnError,
    OcrError,
    VerificationError,
)
from ..formatter import compose_text, contains_ocr_marker, make_footer
from ..journal import Journal
from ..models import DocumentSnapshot, InventoryItem
from ..ocr.base import OcrBackend
from ..verifier import verify_update


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DocumentReferenceHandler:
    def __init__(
        self,
        database: DatabaseReader,
        cdn: CdnClient,
        aps: ApsClient,
        ocr: OcrBackend,
        journal: Journal,
        *,
        report: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._database = database
        self._cdn = cdn
        self._aps = aps
        self._ocr = ocr
        self._journal = journal
        self._report = report or (lambda message: None)

    @staticmethod
    def supports(item: InventoryItem) -> bool:
        return item.class_id == 60 and is_pdf_file_info(item.filename, item.mime_type)

    def _base_record(self, item: InventoryItem) -> dict[str, Any]:
        return {
            "version": __version__,
            "patientNumber": item.patient_number,
            "objectId": item.object_id,
            "classId": item.class_id,
            "filename": item.filename,
            "mimeType": item.mime_type,
            "cdnVerweis": item.cdn_reference,
        }

    def _write(
        self,
        item: InventoryItem,
        status: str,
        *,
        before: Optional[DocumentSnapshot] = None,
        after: Optional[DocumentSnapshot] = None,
        ocr_text: Optional[str] = None,
        new_text: Optional[str] = None,
        downloaded_bytes: Optional[int] = None,
        error: Optional[BaseException] = None,
    ) -> str:
        record = self._base_record(item)
        record.update(
            {
                "revisionBefore": before.revision if before else item.revision,
                "revisionAfter": after.revision if after else None,
                "oldText": before.text if before else None,
                "newTextSha256": _sha256_text(new_text) if new_text is not None else None,
                "ocrChars": len(ocr_text) if ocr_text is not None else None,
                "downloadedBytes": downloaded_bytes,
                "status": status,
            }
        )
        if error is not None:
            record["errorType"] = type(error).__name__
            record["error"] = str(error)[:2000]
        self._journal.write(record)
        self._report(f"{item.patient_number} / {item.object_id}: {status}")
        return status

    def _read_current(self, item: InventoryItem) -> DocumentSnapshot:
        revision = self._database.current_revision(item.object_id)
        return self._aps.find(item.object_id, revision)

    def process(self, item: InventoryItem, *, apply: bool) -> str:
        if not self.supports(item):
            return self._write(item, "unsupported_type")

        try:
            initial = self._read_current(item)
        except (DatabaseReadError, ApsFindError) as exc:
            return self._write(item, "aps_find_failed", error=exc)

        if contains_ocr_marker(initial.text):
            return self._write(item, "already_ocr", before=initial)

        downloaded_bytes: Optional[int] = None
        with tempfile.TemporaryDirectory(prefix="kienzledoku-ocr-document-") as tmp:
            document_path = Path(tmp) / "document.pdf"
            try:
                downloaded_bytes = self._cdn.download(item.cdn_reference, document_path)
                if downloaded_bytes == 0:
                    raise HttpRequestError("CDN lieferte eine leere Datei")
            except MissingCdnError as exc:
                return self._write(item, "missing_cdn", before=initial, error=exc)
            except HttpRequestError as exc:
                return self._write(item, "download_failed", before=initial, error=exc)

            try:
                ocr_text = self._ocr.extract_text(
                    document_path, item.mime_type or "application/pdf"
                )
            except OcrError as exc:
                return self._write(
                    item,
                    "ocr_failed",
                    before=initial,
                    downloaded_bytes=downloaded_bytes,
                    error=exc,
                )
            except Exception as exc:
                return self._write(
                    item,
                    "ocr_failed",
                    before=initial,
                    downloaded_bytes=downloaded_bytes,
                    error=exc,
                )

        if not ocr_text.strip():
            return self._write(
                item,
                "ocr_empty",
                before=initial,
                ocr_text=ocr_text,
                downloaded_bytes=downloaded_bytes,
            )

        if not apply:
            footer = make_footer(__version__)
            new_text = compose_text(initial.text, ocr_text, footer)
            return self._write(
                item,
                "dry_run",
                before=initial,
                ocr_text=ocr_text,
                new_text=new_text,
                downloaded_bytes=downloaded_bytes,
            )

        # OCR may take time. Re-read the full latest DTO immediately before update.
        try:
            current = self._read_current(item)
        except (DatabaseReadError, ApsFindError) as exc:
            return self._write(
                item,
                "aps_find_failed",
                before=initial,
                ocr_text=ocr_text,
                downloaded_bytes=downloaded_bytes,
                error=exc,
            )
        if contains_ocr_marker(current.text):
            return self._write(
                item,
                "already_ocr",
                before=current,
                ocr_text=ocr_text,
                downloaded_bytes=downloaded_bytes,
            )

        footer = make_footer(__version__)
        new_text = compose_text(current.text, ocr_text, footer)

        # Write-ahead record: oldText is durable before APS is changed.
        self._write(
            item,
            "update_prepared",
            before=current,
            ocr_text=ocr_text,
            new_text=new_text,
            downloaded_bytes=downloaded_bytes,
        )
        try:
            self._aps.update_text(current, new_text)
        except ApsUpdateError as exc:
            return self._write(
                item,
                "aps_update_failed",
                before=current,
                ocr_text=ocr_text,
                new_text=new_text,
                downloaded_bytes=downloaded_bytes,
                error=exc,
            )

        try:
            after = self._read_current(item)
            verify_update(current, after, new_text, footer)
        except (DatabaseReadError, ApsFindError, VerificationError) as exc:
            return self._write(
                item,
                "verification_failed",
                before=current,
                ocr_text=ocr_text,
                new_text=new_text,
                downloaded_bytes=downloaded_bytes,
                error=exc,
            )

        return self._write(
            item,
            "updated",
            before=current,
            after=after,
            ocr_text=ocr_text,
            new_text=new_text,
            downloaded_bytes=downloaded_bytes,
        )
