import os
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kienzledoku_ocr_backfill.config import T2medConfig
from kienzledoku_ocr_backfill.db_reader import DatabaseReader, INVENTORY_SQL
from kienzledoku_ocr_backfill.journal import Journal
from kienzledoku_ocr_backfill.ocr.command import CommandOcrBackend
from kienzledoku_ocr_backfill.ocr.ocrmypdf import OcrmypdfBackend


class DatabaseReaderTests(unittest.TestCase):
    CSV = (
        "patientennummer,objectid,revision,classid,gueltigkeitszeitpunkt,verweis,name,mimetype,groesse\n"
        "8100,doc1,2,60,2026-01-01,cdn://APS/Praxis/Patient/a,test.pdf,application/pdf,42\n"
        "8200,doc2,3,60,2026-01-02,cdn://APS/Praxis/Patient/b,other.pdf,application/pdf,99\n"
    )

    def test_inventory_filters_without_dynamic_sql(self):
        reader = DatabaseReader(T2medConfig())
        with mock.patch.object(reader, "_run", return_value=self.CSV) as run:
            items = reader.inventory(patient_number="8200", limit=1)
        self.assertEqual([item.object_id for item in items], ["doc2"])
        self.assertEqual(run.call_args.args[0], INVENTORY_SQL)
        self.assertIn("v.classid = 60", INVENTORY_SQL)
        self.assertNotIn("UPDATE", INVENTORY_SQL.upper())
        self.assertNotIn("DELETE", INVENTORY_SQL.upper())

    def test_limit_counts_supported_pdfs_not_other_document_types(self):
        csv_text = (
            "patientennummer,objectid,revision,classid,gueltigkeitszeitpunkt,verweis,name,mimetype,groesse\n"
            "100,not-pdf,1,60,2026-01-01,cdn://a,letter.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,10\n"
            "100,pdf-by-mime,1,60,2026-01-02,cdn://b,scan,application/pdf,20\n"
            "100,pdf-by-name,1,60,2026-01-03,cdn://c,scan.PDF,application/octet-stream,30\n"
        )
        reader = DatabaseReader(T2medConfig())
        with mock.patch.object(reader, "_run", return_value=csv_text):
            items = reader.inventory(limit=1)
        self.assertEqual([item.object_id for item in items], ["pdf-by-mime"])


class CommandOcrTests(unittest.TestCase):
    def test_stdout_backend(self):
        backend = CommandOcrBackend(
            [sys.executable, "-c", "print('Erkannter Text')", "{input}"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.pdf"
            path.write_bytes(b"PDF")
            self.assertEqual(
                backend.extract_text(path, "application/pdf"),
                "Erkannter Text\n",
            )

    def test_output_file_backend(self):
        backend = CommandOcrBackend(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[2]).write_text('Datei-OCR', encoding='utf-8')",
                "{input}",
                "{output}",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.pdf"
            path.write_bytes(b"PDF")
            self.assertEqual(backend.extract_text(path, "application/pdf"), "Datei-OCR")


class OcrmypdfBackendTests(unittest.TestCase):
    def test_matches_confirmed_kienzlefax_pipeline_and_extracts_full_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argument_log = root / "ocr-arguments.json"
            fake_ocr = root / "ocrmypdf"
            fake_ocr.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "if '--help' in sys.argv:\n"
                "    print('usage: ocrmypdf --mode MODE')\n"
                "    raise SystemExit(0)\n"
                f"Path({str(argument_log)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                "Path(sys.argv[-1]).write_bytes(b'%PDF-fake')\n",
                encoding="utf-8",
            )
            fake_ocr.chmod(0o755)
            fake_pdftotext = root / "pdftotext"
            fake_pdftotext.write_text(
                "#!/usr/bin/env python3\n"
                "print('Vollständiger OCR- und PDF-Text')\n",
                encoding="utf-8",
            )
            fake_pdftotext.chmod(0o755)
            source = root / "input.pdf"
            source.write_bytes(b"%PDF-input")

            backend = OcrmypdfBackend(
                ocrmypdf=str(fake_ocr),
                pdftotext=str(fake_pdftotext),
                rotate_pages_threshold=2.0,
            )
            text = backend.extract_text(source, "application/pdf")

            self.assertEqual(text, "Vollständiger OCR- und PDF-Text\n")
            arguments = json.loads(argument_log.read_text(encoding="utf-8"))
            for expected in (
                "--mode",
                "skip",
                "deu+eng",
                "--tesseract-oem",
                "--rotate-pages",
                "--rotate-pages-threshold",
                "2",
                "--deskew",
                "--clean",
                "--oversample",
                "300",
                "pdfa-3",
                "--optimize",
                "--tesseract-timeout",
                "--jobs",
                "2",
            ):
                self.assertIn(expected, arguments)

            threshold_index = arguments.index("--rotate-pages-threshold")
            self.assertEqual(arguments[threshold_index + 1], "2")


class JournalTests(unittest.TestCase):
    def test_journal_is_0600_fsynced_and_resume_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            with Journal(path) as journal:
                journal.write({"objectId": "one", "status": "updated", "oldText": "Alt"})
                journal.write({"objectId": "two", "status": "ocr_failed"})
                self.assertEqual(journal.completed_object_ids(), {"one"})
                journal.write({"objectId": "one", "status": "rolled_back"})
                self.assertEqual(journal.completed_object_ids(), set())
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
