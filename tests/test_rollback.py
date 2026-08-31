import hashlib
import tempfile
import unittest
from pathlib import Path

from kienzledoku_ocr_backfill.journal import Journal
from kienzledoku_ocr_backfill.models import DocumentSnapshot
from kienzledoku_ocr_backfill.rollback import RollbackProcessor


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeAps:
    def __init__(self, text, revision):
        self.text = text
        self.revision = revision
        self.updates = 0

    def find(self, object_id, revision):
        dto = {
            "ref": {"objectId": {"id": object_id}, "revision": revision},
            "text": self.text,
            "verweis": "cdn://same",
            "gueltigkeitszeitpunkt": 123,
            "fachinformationstyp": 75,
        }
        return DocumentSnapshot(dto, object_id, revision, self.text)

    def update_text(self, current, new_text):
        self.text = new_text
        self.revision += 1
        self.updates += 1
        return {"successful": True}


class FakeDb:
    def __init__(self, aps):
        self.aps = aps

    def current_revision(self, object_id):
        return self.aps.revision


def updated_record(current="OCR text", old="Old text", revision=4):
    return {
        "version": "1.00",
        "patientNumber": "8100",
        "objectId": "doc1",
        "classId": 60,
        "filename": "test.pdf",
        "mimeType": "application/pdf",
        "cdnVerweis": "cdn://same",
        "revisionBefore": revision - 1,
        "revisionAfter": revision,
        "oldText": old,
        "newTextSha256": sha(current),
        "status": "updated",
    }


class RollbackTests(unittest.TestCase):
    def test_rollback_dry_run_does_not_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps("OCR text", 4)
            with Journal(path) as journal:
                journal.write(updated_record())
                summary = RollbackProcessor(FakeDb(aps), aps, journal).run(apply=False)
            self.assertEqual(summary.counts, {"rollback_dry_run": 1})
            self.assertEqual(aps.updates, 0)

    def test_exact_revision_and_hash_allow_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps("OCR text", 4)
            with Journal(path) as journal:
                journal.write(updated_record())
                summary = RollbackProcessor(FakeDb(aps), aps, journal).run(apply=True)
                self.assertNotIn("doc1", journal.completed_object_ids())
            self.assertEqual(summary.counts, {"rolled_back": 1})
            self.assertEqual(aps.text, "Old text")
            self.assertEqual(aps.updates, 1)

    def test_manual_text_change_is_conflict_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            aps = FakeAps("Manually changed", 5)
            with Journal(path) as journal:
                journal.write(updated_record())
                summary = RollbackProcessor(FakeDb(aps), aps, journal).run(apply=True)
            self.assertEqual(summary.counts, {"rollback_conflict": 1})
            self.assertEqual(aps.text, "Manually changed")
            self.assertEqual(aps.updates, 0)


if __name__ == "__main__":
    unittest.main()
