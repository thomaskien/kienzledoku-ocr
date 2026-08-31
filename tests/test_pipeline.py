import json
import tempfile
import unittest
from pathlib import Path

from kienzledoku_ocr_backfill.errors import MissingCdnError
from kienzledoku_ocr_backfill.handlers.document_reference import DocumentReferenceHandler
from kienzledoku_ocr_backfill.journal import Journal
from kienzledoku_ocr_backfill.models import DocumentSnapshot, InventoryItem
from kienzledoku_ocr_backfill.processor import BackfillProcessor


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
            self.assertIn("kienzledoku OCR v1.00,", aps.text)

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
