import base64
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kienzledoku_ocr_backfill.bfarm_pzn import (
    ImportFormatError,
    PZNResolver,
    build_database,
    extract_release_zip,
    find_release_files,
    normalize_pzn,
    update_database,
)
from kienzledoku_ocr_backfill.bmp import BmpParseError, format_bmp, parse_bmp
from kienzledoku_ocr_backfill.medication_plans import MedicationPlanScan, MedicationPlanScanner
from kienzledoku_ocr_backfill.ocr.ocrmypdf import OcrmypdfBackend
from kienzledoku_ocr_backfill.qr_extractor import (
    QrCode,
    QrExtractionResult,
    _decode_image,
    extract_qr_codes,
)


BMP_XML = (
    '<MP v="028" U="ABCDEF" l="de-DE">'
    '<P g="Jörg" f="Muster" egk="X123" b="19750101" s="M"/>'
    '<A n="Dr. Test" s="Testweg 1" z="24100" c="Kiel" '
    't="2026-08-31T10:15:00"/>'
    '<O w="80" ai="Penicillin"/>'
    '<S c="412"><M p="9322739" m="1" v="1/2" du="1" '
    'i="nach dem Essen" r="Bluthochdruck"/>'
    '<X t="Kontrolle in vier Wochen"/></S></MP>'
)


def qr_code(data, page=1, retry=False):
    return QrCode(
        page=page,
        code_type="DataMatrix",
        data=data,
        text=None,
        rect={"left": 10, "top": 20, "width": 30, "height": 40},
        polygon=(),
        dpi=600 if retry else 300,
        retry=retry,
    )


class BmpTests(unittest.TestCase):
    def test_parse_and_format_with_pzn_resolution(self):
        class Resolver:
            def lookup(self, pzn):
                return {
                    "name": "Testmed 10 mg",
                    "form_long": "Tablette",
                    "form_short": "Tabl.",
                    "substances": [{"name": "Teststoff", "strength": "10 mg"}],
                }

        plan = parse_bmp(BMP_XML.encode("iso-8859-1"))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.patient["given"], "Jörg")
        formatted = format_bmp(plan, Resolver())
        self.assertTrue(formatted.text.startswith("----- BEGINN BUNDESMEDIKATIONSPLAN -----"))
        self.assertIn(
            "BUNDESMEDIKATIONSPLAN für Jörg Muster, Geburtsdatum: 01.01.1975",
            formatted.text,
        )
        self.assertIn("Ausstellungsdatum: 31.08.2026 10:15", formatted.text)
        self.assertIn("Ausgestellt durch: Dr. Test", formatted.text)
        self.assertIn("Überschrift: Dauermedikation", formatted.text)
        self.assertIn("## Nr\t| Medikament\t| Dosis\t| Einnahme", formatted.text)
        self.assertIn("1\t| Testmed 10 mg (Teststoff)\t| 10 mg, Tablette\t| 1-0-1/2-0 Stück", formatted.text)
        self.assertIn("Kommentar: nach dem Essen; Grund: Bluthochdruck; PZN 09322739", formatted.text)
        self.assertIn("Tablette", formatted.text)
        self.assertIn("nach dem Essen", formatted.text)
        self.assertIn("Kontrolle in vier Wochen", formatted.text)
        self.assertTrue(formatted.text.endswith("----- ENDE BUNDESMEDIKATIONSPLAN -----"))
        self.assertEqual(formatted.unresolved_pzns, ())

    def test_free_text_dosage_is_used_as_intake(self):
        payload = (
            '<MP v="028"><P g="Erika" f="Muster"/>'
            '<S t="Bedarf"><M a="Schmerzmittel" t="bei Bedarf" dud="Tropfen"/></S>'
            '</MP>'
        )
        formatted = format_bmp(parse_bmp(payload.encode("iso-8859-1")))
        self.assertIn(
            "1\t| Schmerzmittel\t| -\t| bei Bedarf Tropfen",
            formatted.text,
        )

    def test_non_bmp_is_ignored_and_entities_are_rejected(self):
        self.assertIsNone(parse_bmp(b"https://example.test/qr"))
        with self.assertRaises(BmpParseError):
            parse_bmp(b'<!DOCTYPE MP [<!ENTITY x "bad">]><MP v="028"><S/></MP>')


class PznResolverTests(unittest.TestCase):
    def _write_source(self, root):
        date = "20260815"
        (root / f"{date}-REFERENCE_MEDICINAL_PRODUCT.dsv").write_text(
            "RMP_KEY|RMP_PZN|RMP_COUNT_SUBSTANCE|RMP_MULTIPLE_PPT|"
            "RMP_PFM_PUT_SHORT|RMP_PFM_PUT_LONG|RMP_PFM_NAME|"
            "RMP_PFM_TERM_ID|RMP_MPD_NAME\n"
            "9322739|9322739|1|0|Tabl.|Tablette|Tabletten|123|Testmed 10 mg\n",
            encoding="utf-8",
        )
        (root / f"{date}-REFERENCE_PHARMACEUTICAL_PRODUCT.dsv").write_text(
            "RPP_KEY|RMP_KEY|RPP_NUMBER|RPP_PFM_PUT_SHORT|RPP_PFM_PUT_LONG|"
            "RPP_PFM_NAME|RPP_PFM_TERM_ID|RPP_DESCRIPTION\n"
            "9322739-1|9322739|1|Tabl.|Tablette|Tabletten|123|\n",
            encoding="utf-8",
        )
        (root / f"{date}-REFERENCE_SUBSTANCE.dsv").write_text(
            "RSE_KEY|RPP_KEY|RSE_SUBSTANCE_NAME|RSE_SUBSTANCE_STRENGTH|"
            "RSE_SUBSTANCE_ID|RSE_SUBSTANCE_RANK\n"
            "9322739-1-1|9322739-1|Teststoff|10 mg|42|1\n",
            encoding="utf-8",
        )

    def test_supplied_downloader_schema_and_read_only_lookup(self):
        self.assertEqual(normalize_pzn("PZN 9322739"), "09322739")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_source(root)
            release, files = find_release_files(root)
            database = root / "bfarm.sqlite"
            counts = build_database(files, database, release_date=release)
            self.assertEqual(counts["products"], 1)
            with PZNResolver(database) as resolver:
                drug = resolver.lookup("9322739")
                self.assertEqual(drug["name"], "Testmed 10 mg")
                self.assertEqual(drug["substances"][0]["name"], "Teststoff")
                self.assertEqual(resolver.metadata()["release_date"], release)

    def test_official_delivery_zip_can_be_imported_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            self._write_source(source)
            archive_path = root / "bfarm-lieferung.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for path in source.iterdir():
                    archive.write(path, arcname=f"Lieferung/{path.name}")

            database = root / "bfarm.sqlite"
            result = update_database(database, source_zip=archive_path)

            self.assertEqual(result["release_date"], "20260815")
            self.assertEqual(result["counts"]["products"], 1)
            self.assertEqual(result["source"], str(archive_path))
            with PZNResolver(database) as resolver:
                self.assertEqual(resolver.lookup("9322739")["name"], "Testmed 10 mg")

    def test_non_delivery_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "resolver.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("README.md", "kein Datenexport")
            with self.assertRaisesRegex(ImportFormatError, "keine vollständige Lieferung"):
                extract_release_zip(archive_path, root / "extracted")


class QrExtractorTests(unittest.TestCase):
    def test_raw_bytes_utf8_fallback_and_geometry(self):
        class Point:
            def __init__(self, x, y):
                self.x, self.y = x, y

        class Position:
            top_left = Point(10, 20)
            top_right = Point(40, 20)
            bottom_right = Point(40, 60)
            bottom_left = Point(10, 60)

        class Format:
            name = "DataMatrix"

        barcode = mock.Mock(bytes=b"\xffBMP", position=Position(), format=Format())
        zxing = mock.Mock()
        zxing.read_barcodes.return_value = [barcode]
        codes = _decode_image(object(), page=2, dpi=300, retry=False, zxingcpp=zxing)
        self.assertEqual(codes[0].data, b"\xffBMP")
        self.assertIsNone(codes[0].text)
        self.assertEqual(codes[0].rect, {"left": 10, "top": 20, "width": 30, "height": 40})
        self.assertEqual(codes[0].as_dict()["base64"], base64.b64encode(b"\xffBMP").decode())

    def test_pdf_pages_retry_independently(self):
        class OpenImage:
            def __init__(self, path):
                self.path = Path(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def load(self):
                pass

        class Images:
            open = OpenImage

        def renderer(command, timeout):
            del timeout
            prefix = Path(command[-1])
            if "-singlefile" in command:
                prefix.with_suffix(".png").write_bytes(b"PNG")
            else:
                Path(str(prefix) + "-1.png").write_bytes(b"PNG1")
                Path(str(prefix) + "-2.png").write_bytes(b"PNG2")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def decoder(image, *, page, dpi, retry, zxingcpp):
            del image, dpi, zxingcpp
            if page == 2 and not retry:
                return [qr_code(b"page2", page=2)]
            if page == 1 and retry:
                return [qr_code(b"page1", page=1, retry=True)]
            return []

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scan.pdf"
            source.write_bytes(b"%PDF-1.7")
            with mock.patch(
                "kienzledoku_ocr_backfill.qr_extractor._dependencies",
                return_value=(Images, object(), object()),
            ), mock.patch(
                "kienzledoku_ocr_backfill.qr_extractor._run_renderer",
                side_effect=renderer,
            ):
                result = extract_qr_codes(source, decoder=decoder)
        self.assertEqual(result.pages_scanned, 2)
        self.assertEqual([(c.page, c.retry) for c in result.codes], [(1, True), (2, False)])
        self.assertEqual(result.errors, ())


class MedicationPlanIntegrationTests(unittest.TestCase):
    def test_scanner_uses_t2med_amdb_and_returns_page_replacement(self):
        class Resolver:
            def __init__(self, **kwargs):
                self.options = kwargs

            def metadata(self):
                return {"schema": "mmidata1", "serverVersion": "11.4.5-MariaDB"}

            def lookup(self, pzn):
                return {"name": "Testmed", "form_long": "Tablette", "form_short": None, "substances": []}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "scan.pdf"
            source.write_bytes(b"%PDF")
            config = root / "service.conf"
            client = root / "mariadb"
            socket = root / "t2med-mariadb"
            extraction = QrExtractionResult(
                str(source), (qr_code(BMP_XML.encode("iso-8859-1")),), (), 2
            )
            with mock.patch(
                "kienzledoku_ocr_backfill.medication_plans.extract_qr_codes",
                return_value=extraction,
            ), mock.patch(
                "kienzledoku_ocr_backfill.medication_plans.T2medAmdbResolver", Resolver
            ):
                scan = MedicationPlanScanner(
                    amdb_config=config,
                    amdb_client=client,
                    amdb_socket=socket,
                ).scan(source)
        self.assertEqual(set(scan.pages), {1})
        self.assertIn("1\t| Testmed", scan.pages[1])
        self.assertIn("BEGINN BUNDESMEDIKATIONSPLAN", scan.pages[1])
        self.assertEqual(scan.diagnostics["amdbSchema"], "mmidata1")
        self.assertEqual(scan.diagnostics["amdbServerVersion"], "11.4.5-MariaDB")

    def test_scanner_falls_back_to_ocr_when_database_is_missing(self):
        extraction = QrExtractionResult(
            "scan.pdf", (qr_code(BMP_XML.encode("iso-8859-1")),), (), 1
        )
        with mock.patch(
            "kienzledoku_ocr_backfill.medication_plans.extract_qr_codes",
            return_value=extraction,
        ):
            scan = MedicationPlanScanner(
                amdb_config=Path("/missing-service.conf"),
                amdb_client=Path("/missing-mariadb"),
                amdb_socket=Path("/missing-socket"),
            ).scan(Path("scan.pdf"))
        self.assertEqual(scan.pages, {})
        self.assertEqual(scan.diagnostics["errors"][-1]["stage"], "t2med_amdb")

    def test_scanner_falls_back_to_ocr_when_lookup_fails(self):
        class Resolver:
            def __init__(self, **kwargs):
                del kwargs

            def metadata(self):
                return {"schema": "mmidata1", "serverVersion": "11.4.5-MariaDB"}

            def lookup(self, pzn):
                raise RuntimeError(f"Lesefehler für {pzn}")

            def close(self):
                pass

        extraction = QrExtractionResult(
            "scan.pdf", (qr_code(BMP_XML.encode("iso-8859-1")),), (), 1
        )
        with mock.patch(
            "kienzledoku_ocr_backfill.medication_plans.extract_qr_codes",
            return_value=extraction,
        ), mock.patch(
            "kienzledoku_ocr_backfill.medication_plans.T2medAmdbResolver", Resolver
        ):
            scan = MedicationPlanScanner().scan(Path("scan.pdf"))
        self.assertEqual(scan.pages, {})
        self.assertEqual(scan.diagnostics["errors"][-1]["stage"], "t2med_amdb_lookup")

    def test_backend_excludes_bmp_page_and_combines_page_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argument_log = root / "arguments.json"
            fake_ocr = root / "ocrmypdf"
            fake_ocr.write_text(
                "#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\n"
                "if '--help' in sys.argv:\n print('--mode --pages'); raise SystemExit(0)\n"
                f"Path({str(argument_log)!r}).write_text(json.dumps(sys.argv))\n"
                "Path(sys.argv[-1]).write_bytes(b'%PDF-output')\n",
                encoding="utf-8",
            )
            fake_ocr.chmod(0o755)
            fake_pdftotext = root / "pdftotext"
            fake_pdftotext.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "page = sys.argv[sys.argv.index('-f') + 1]\n"
                "print('OCR-Text Seite ' + page)\n",
                encoding="utf-8",
            )
            fake_pdftotext.chmod(0o755)
            source = root / "input.pdf"
            source.write_bytes(b"%PDF-input")
            backend = OcrmypdfBackend(
                ocrmypdf=str(fake_ocr), pdftotext=str(fake_pdftotext), auto_orient_pages=False
            )
            scan = MedicationPlanScan(
                {1: "STRUKTURIERTER MEDIKATIONSPLAN"}, 2, {"plans": [{"page": 1}]}
            )
            with mock.patch.object(backend, "_scan_medication_plans", return_value=scan):
                text = backend.extract_text(source, "application/pdf")
            arguments = json.loads(argument_log.read_text(encoding="utf-8"))
        pages_index = arguments.index("--pages")
        self.assertEqual(arguments[pages_index + 1], "2")
        self.assertEqual(text, "STRUKTURIERTER MEDIKATIONSPLAN\n\nOCR-Text Seite 2")

    def test_backend_skips_ocr_when_every_page_is_bmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.pdf"
            source.write_bytes(b"%PDF-input")
            backend = OcrmypdfBackend(auto_orient_pages=False)
            scan = MedicationPlanScan({1: "BMP SEITE 1"}, 1, {"plans": [{"page": 1}]})
            with mock.patch.object(backend, "_scan_medication_plans", return_value=scan):
                text = backend.extract_text(source, "application/pdf")
        self.assertEqual(text, "BMP SEITE 1")
        self.assertEqual(backend.diagnostics()["medicationPlans"], scan.diagnostics)


if __name__ == "__main__":
    unittest.main()
