import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from kienzledoku_ocr_backfill.errors import MissingCdnError
from kienzledoku_ocr_backfill.handlers.document_reference import DocumentReferenceHandler
from kienzledoku_ocr_backfill.journal import Journal
from kienzledoku_ocr_backfill.models import DocumentSnapshot, InventoryItem
from kienzledoku_ocr_backfill.processor import BackfillProcessor
from kienzledoku_ocr_backfill.formatter import compose_text


def item(object_id="doc1", patient="8100"):
    return InventoryItem(
        patient_number=patient,
        object_id=object_id,
        revision=0,
        class_id=60,
        valid_at="2026-01-01",
        cdn_reference=f"cdn://APS/Praxis/Patient/{object_id}",
        filename=f"{object_id}.pdf",
        mime_type="application/pdf",
        size=3,
    )


class FakeDatabase:
    def __init__(self, aps):
        self.aps = aps

    def current_revision(self, object_id):
        return self.aps.revision


class FakeAps:
    def __init__(self, text="Alter Titel"):
        self.text = text
        self.revision = 0
        self.update_calls = 0

    def find(self, object_id, revision):
        if revision != self.revision:
            raise AssertionError("stale revision")
        dto = {
            "ref": {"objectId": {"id": object_id}, "revision": revision},
            "text": self.text,
            "verweis": f"cdn://APS/Praxis/Patient/{object_id}",
            "gueltigkeitszeitpunkt": 123,
            "fachinformationstyp": 75,
            "kuerzel": "PDF",
            "unzugeordnet": False,
        }
        return DocumentSnapshot(dto, object_id, revision, self.text)

    def update_text(self, current, new_text):
        self.update_calls += 1
        self.text = new_text
        self.revision += 1
        return {"successful": True}


class FakeCdn:
    def __init__(self, missing_ids=None):
        self.missing_ids = set(missing_ids or [])
        self.calls = []

    def download(self, reference, target):
        object_id = reference.rsplit("/", 1)[-1]
        self.calls.append(object_id)
        if object_id in self.missing_ids:
            raise MissingCdnError("missing", status=404)
        target.write_bytes(b"PDF")
        return 3


class FakeOcr:
    def __init__(self, text="Erkannter Text"):
        self.text = text
        self.calls = 0

    def extract_text(self, path, mime_type):
        self.calls += 1
        return self.text


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def text_sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PipelineTests(unittest.TestCase):
    def test_dry_run_never_updates_aps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps()
            with Journal(path) as journal:
                handler = DocumentReferenceHandler(
                    FakeDatabase(aps), FakeCdn(), aps, FakeOcr(), journal
                )
                status = handler.process(item(), apply=False)
            self.assertEqual(status, "dry_run")
            self.assertEqual(aps.update_calls, 0)
            record = read_records(path)[0]
            self.assertEqual(record["oldText"], "Alter Titel")
            self.assertEqual(record["ocrChars"], len("Erkannter Text"))
            self.assertEqual(len(record["newTextSha256"]), 64)

    def test_apply_writes_old_text_before_update_then_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps()
            with Journal(path) as journal:
                handler = DocumentReferenceHandler(
                    FakeDatabase(aps), FakeCdn(), aps, FakeOcr(), journal
                )
                status = handler.process(item(), apply=True)
            self.assertEqual(status, "updated")
            self.assertEqual(aps.update_calls, 1)
            records = read_records(path)
            self.assertEqual([record["status"] for record in records], ["update_prepared", "updated"])
            self.assertEqual(records[0]["oldText"], "Alter Titel")
            self.assertEqual(records[1]["revisionBefore"], 0)
            self.assertEqual(records[1]["revisionAfter"], 1)
            self.assertIn("----- BEGINN kienzledoku OCR -----", aps.text)
            self.assertIn("kienzledoku OCR v1.1,", aps.text)
            self.assertTrue(aps.text.endswith("----- ENDE kienzledoku OCR -----"))

    def test_existing_marker_skips_download_and_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps("Alt\n\nkienzledoku OCR v1.00, 30.08.2026 14:55")
            cdn = FakeCdn()
            ocr = FakeOcr()
            with Journal(path) as journal:
                handler = DocumentReferenceHandler(FakeDatabase(aps), cdn, aps, ocr, journal)
                status = handler.process(item(), apply=True)
            self.assertEqual(status, "already_ocr")
            self.assertEqual(cdn.calls, [])
            self.assertEqual(ocr.calls, 0)
            self.assertEqual(aps.update_calls, 0)

    def test_reprocess_replaces_complete_managed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            previous = compose_text(
                "Alter Titel",
                "Schlechter alter OCR-Text",
                "kienzledoku OCR v1.1, 31.08.2026 10:00",
            )
            aps = FakeAps(previous)
            with Journal(path) as journal:
                handler = DocumentReferenceHandler(
                    FakeDatabase(aps),
                    FakeCdn(),
                    aps,
                    FakeOcr("Verbesserter OCR-Text"),
                    journal,
                    reprocess_existing=True,
                )
                status = handler.process(item(), apply=True)
            self.assertEqual(status, "updated")
            self.assertEqual(aps.update_calls, 1)
            self.assertTrue(aps.text.startswith("Alter Titel\n\n\n"))
            self.assertIn("Verbesserter OCR-Text", aps.text)
            self.assertNotIn("Schlechter alter OCR-Text", aps.text)
            self.assertEqual(aps.text.count("----- BEGINN kienzledoku OCR -----"), 1)

    def test_reprocess_legacy_block_uses_matching_verified_journal_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            legacy = (
                "Alter Titel\n\n\nAlter OCR-Text\n\n"
                "kienzledoku OCR v1.00, 30.08.2026 14:55"
            )
            aps = FakeAps(legacy)
            with Journal(path) as journal:
                journal.write(
                    {
                        "version": "1.00",
                        "objectId": "doc1",
                        "status": "updated",
                        "oldText": "Alter Titel",
                        "newTextSha256": text_sha256(legacy),
                    }
                )
                handler = DocumentReferenceHandler(
                    FakeDatabase(aps),
                    FakeCdn(),
                    aps,
                    FakeOcr("OCR aus Version 1.1"),
                    journal,
                    reprocess_existing=True,
                )
                status = handler.process(item(), apply=True)
            self.assertEqual(status, "updated")
            self.assertTrue(aps.text.startswith("Alter Titel\n\n\n"))
            self.assertNotIn("Alter OCR-Text", aps.text)
            self.assertIn("OCR aus Version 1.1", aps.text)

    def test_reprocess_legacy_block_without_matching_journal_is_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            legacy = "Titel\n\nOCR\n\nkienzledoku OCR v1.00, 30.08.2026 14:55"
            aps = FakeAps(legacy)
            cdn = FakeCdn()
            with Journal(path) as journal:
                handler = DocumentReferenceHandler(
                    FakeDatabase(aps),
                    cdn,
                    aps,
                    FakeOcr(),
                    journal,
                    reprocess_existing=True,
                )
                status = handler.process(item(), apply=True)
            self.assertEqual(status, "reprocess_conflict")
            self.assertEqual(cdn.calls, [])
            self.assertEqual(aps.update_calls, 0)

    def test_missing_cdn_does_not_stop_next_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps()
            cdn = FakeCdn(missing_ids={"missing"})
            with Journal(path) as journal:
                handler = DocumentReferenceHandler(
                    FakeDatabase(aps), cdn, aps, FakeOcr(), journal
                )
                summary = BackfillProcessor(handler, journal).run(
                    [item("missing"), item("present")],
                    apply=False,
                    resume=False,
                )
            self.assertEqual(summary.counts, {"missing_cdn": 1, "dry_run": 1})
            self.assertEqual(cdn.calls, ["missing", "present"])

    def test_resume_skips_only_completed_object_ids(self):
        class NeverCalled:
            def process(self, item, apply):
                raise AssertionError("handler must not be called")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            with Journal(path) as journal:
                journal.write({"objectId": "doc1", "status": "updated"})
                summary = BackfillProcessor(NeverCalled(), journal).run(
                    [item("doc1")], apply=True, resume=True
                )
            self.assertEqual(summary.counts, {"resume_skipped": 1})


if __name__ == "__main__":
    unittest.main()
