import os
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kienzledoku_ocr_backfill.cli import parse_args
from kienzledoku_ocr_backfill.config import T2medConfig
from kienzledoku_ocr_backfill.db_reader import DatabaseReader, INVENTORY_SQL
from kienzledoku_ocr_backfill.journal import Journal
from kienzledoku_ocr_backfill.ocr.command import CommandOcrBackend
from kienzledoku_ocr_backfill.ocr.ocrmypdf import OcrmypdfBackend


class DatabaseReaderTests(unittest.TestCase):
    CSV = (
        "patientennummer,patientenname,objectid,revision,classid,gueltigkeitszeitpunkt,verweis,name,mimetype,groesse\n"
        "8100,Erika Muster,doc1,2,60,2026-01-01,cdn://APS/Praxis/Patient/a,test.pdf,application/pdf,42\n"
        "8200,Max Beispiel,doc2,3,60,2026-01-02,cdn://APS/Praxis/Patient/b,other.pdf,application/pdf,99\n"
    )

    def test_inventory_filters_without_dynamic_sql(self):
        reader = DatabaseReader(T2medConfig())
        with mock.patch.object(reader, "_run", return_value=self.CSV) as run:
            items = reader.inventory(patient_number="8200", limit=1)
        self.assertEqual([item.object_id for item in items], ["doc2"])
        self.assertEqual(items[0].patient_name, "Max Beispiel")
        self.assertEqual(run.call_args.args[0], INVENTORY_SQL)
        self.assertIn("v.classid = 60", INVENTORY_SQL)
        self.assertNotIn("UPDATE", INVENTORY_SQL.upper())
        self.assertNotIn("DELETE", INVENTORY_SQL.upper())

    def test_limit_counts_supported_pdfs_not_other_document_types(self):
        csv_text = (
            "patientennummer,patientenname,objectid,revision,classid,gueltigkeitszeitpunkt,verweis,name,mimetype,groesse\n"
            "100,,not-pdf,1,60,2026-01-01,cdn://a,letter.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,10\n"
            "100,,pdf-by-mime,1,60,2026-01-02,cdn://b,scan,application/pdf,20\n"
            "100,,pdf-by-name,1,60,2026-01-03,cdn://c,scan.PDF,application/octet-stream,30\n"
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


class CliTests(unittest.TestCase):
    def test_forced_page_rotations_are_parsed_and_repeatable(self):
        args = parse_args(
            [
                "--force-rotate-page",
                "1:+90",
                "--force-rotate-page",
                "3:-90",
            ]
        )
        self.assertEqual(args.force_rotate_page, [(1, "+90"), (3, "-90")])

    def test_t2med_amdb_defaults_and_overrides_are_parsed(self):
        defaults = parse_args([])
        self.assertEqual(
            defaults.amdb_config,
            Path("/opt/t2med/server/mmi/service.conf"),
        )
        self.assertEqual(
            defaults.amdb_client,
            Path("/opt/t2med/server/mariadb/bin/mariadb"),
        )
        self.assertEqual(
            defaults.amdb_socket,
            Path("/var/opt/t2med/data/mariadb/t2med-mariadb"),
        )
        custom = parse_args(
            [
                "--amdb-config",
                "/tmp/service.conf",
                "--amdb-client",
                "/tmp/mariadb",
                "--amdb-socket",
                "/tmp/socket",
                "--amdb-timeout",
                "12.5",
            ]
        )
        self.assertEqual(custom.amdb_config, Path("/tmp/service.conf"))
        self.assertEqual(custom.amdb_client, Path("/tmp/mariadb"))
        self.assertEqual(custom.amdb_socket, Path("/tmp/socket"))
        self.assertEqual(custom.amdb_timeout, 12.5)


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
            qpdf_argument_log = root / "qpdf-arguments.json"
            fake_qpdf = root / "qpdf"
            fake_qpdf.write_text(
                "#!/usr/bin/env python3\n"
                "import json, shutil, sys\n"
                "from pathlib import Path\n"
                f"Path({str(qpdf_argument_log)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                "shutil.copyfile(sys.argv[1], sys.argv[2])\n",
                encoding="utf-8",
            )
            fake_qpdf.chmod(0o755)
            source = root / "input.pdf"
            source.write_bytes(b"%PDF-input")
            progress = []

            backend = OcrmypdfBackend(
                ocrmypdf=str(fake_ocr),
                pdftotext=str(fake_pdftotext),
                qpdf=str(fake_qpdf),
                rotate_pages_threshold=2.0,
                forced_page_rotations=((1, "+90"),),
                auto_orient_pages=False,
                progress=progress.append,
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
            self.assertEqual(source.read_bytes(), b"%PDF-input")
            qpdf_arguments = json.loads(qpdf_argument_log.read_text(encoding="utf-8"))
            self.assertIn("--rotate=+90:1", qpdf_arguments)
            self.assertIn("--flatten-rotation", qpdf_arguments)
            self.assertNotEqual(arguments[-2], str(source))
            self.assertTrue(arguments[-2].endswith("oriented.pdf"))
            for label in (
                "Dauer Orientierungsprüfung:",
                "Dauer Data-Matrix/BMP-Prüfung:",
                "Dauer OCRmyPDF:",
                "Dauer Textextraktion/Zusammenführung:",
            ):
                self.assertTrue(
                    any(message.startswith(label) for message in progress),
                    label,
                )
            self.assertEqual(
                set(backend.diagnostics()["timingsSeconds"]),
                {"orientation", "medicationPlanScan", "ocrmypdf", "textExtraction"},
            )

    def test_osd_orients_each_page_and_records_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = []
            fake_pdftoppm = root / "pdftoppm"
            fake_pdftoppm.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "from pathlib import Path\n"
                "prefix = Path(sys.argv[-1])\n"
                "if '-singlefile' in sys.argv:\n"
                "    prefix.with_suffix('.png').write_bytes(b'PNG')\n"
                "else:\n"
                "    Path(str(prefix) + '-1.png').write_bytes(b'PAGE1')\n"
                "    Path(str(prefix) + '-2.png').write_bytes(b'PAGE2')\n",
                encoding="utf-8",
            )
            fake_pdftoppm.chmod(0o755)
            fake_tesseract = root / "tesseract"
            fake_tesseract.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "name = sys.argv[1]\n"
                "if name.endswith('-1.png'):\n"
                "    print('Orientation in degrees: 270')\n"
                "    print('Rotate: 90')\n"
                "    print('Orientation confidence: 10.79')\n"
                "else:\n"
                "    print('Orientation in degrees: 0')\n"
                "    print('Rotate: 0')\n"
                "    print('Orientation confidence: 11.70')\n",
                encoding="utf-8",
            )
            fake_tesseract.chmod(0o755)
            qpdf_log = root / "qpdf-log.json"
            fake_qpdf = root / "qpdf"
            fake_qpdf.write_text(
                "#!/usr/bin/env python3\n"
                "import json, shutil, sys\n"
                "from pathlib import Path\n"
                f"Path({str(qpdf_log)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                "shutil.copyfile(sys.argv[1], sys.argv[2])\n",
                encoding="utf-8",
            )
            fake_qpdf.chmod(0o755)
            source = root / "input.pdf"
            source.write_bytes(b"%PDF-input")

            backend = OcrmypdfBackend(
                pdftoppm=str(fake_pdftoppm),
                qpdf=str(fake_qpdf),
                tesseract=str(fake_tesseract),
                progress=progress.append,
            )
            with tempfile.TemporaryDirectory() as work:
                oriented = backend._prepare_ocr_input(source, Path(work))
                self.assertEqual(oriented.read_bytes(), b"%PDF-input")

            qpdf_arguments = json.loads(qpdf_log.read_text(encoding="utf-8"))
            self.assertIn("--rotate=+90:1", qpdf_arguments)
            self.assertNotIn("--rotate=+90:2", qpdf_arguments)
            decisions = backend.diagnostics()["pageOrientations"]
            self.assertEqual(
                [(entry["page"], entry["rotation"], entry["method"]) for entry in decisions],
                [(1, "+90", "osd"), (2, "0", "osd")],
            )
            self.assertIn("Orientierung Seite 1: +90° (osd, Konfidenz 10.79)", progress)
            self.assertIn("Orientierung Seite 2: 0° (osd, Konfidenz 11.7)", progress)

    def test_four_way_score_rewards_medication_vocabulary(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        ordinary = header + "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t70\txyz\n"
        medication = (
            header
            + "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t70\tMedikationsplan\n"
            + "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t70\tWirkstoff\n"
        )
        self.assertGreater(
            OcrmypdfBackend._score_tsv(medication.encode())["score"],
            OcrmypdfBackend._score_tsv(ordinary.encode())["score"],
        )

    def test_four_way_fallback_selects_clear_best_rotation(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        weak = header + "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t20\tx\n"
        strong = (
            header
            + "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t85\tMedikationsplan\n"
            + "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t85\tWirkstoff\n"
            + "5\t1\t1\t1\t1\t3\t0\t0\t1\t1\t85\tTabletten\n"
        )
        backend = OcrmypdfBackend()

        def candidate(_source, _original, _page, angle, tmp):
            return tmp / f"candidate-{angle}.png"

        def tesseract(command, *, timeout):
            del timeout
            payload = strong if command[1].endswith("candidate-90.png") else weak
            return mock.Mock(returncode=0, stdout=payload.encode(), stderr=b"")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(backend, "_candidate_image", side_effect=candidate):
                with mock.patch(
                    "kienzledoku_ocr_backfill.ocr.ocrmypdf._run",
                    side_effect=tesseract,
                ):
                    decision = backend._four_way_decision(
                        Path(tmp) / "source.pdf",
                        Path(tmp) / "page.png",
                        1,
                        1.5,
                        Path(tmp),
                    )

        self.assertEqual(decision.rotation, "+90")
        self.assertEqual(decision.method, "four_way")
        self.assertEqual(decision.status, "rotated")
        self.assertGreater(decision.scores["margin"], 4.0)


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
