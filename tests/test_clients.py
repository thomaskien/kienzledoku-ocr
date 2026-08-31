import json
import tempfile
import unittest
from pathlib import Path

from kienzledoku_ocr_backfill.aps_client import ApsClient
from kienzledoku_ocr_backfill.cdn_client import CdnClient, content_path_from_reference
from kienzledoku_ocr_backfill.errors import MissingCdnError
from kienzledoku_ocr_backfill.http_client import HttpResult


class FakeHttp:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.results.pop(0)


def result(payload, status=200, url="https://t2med.test"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return HttpResult(status, {}, body, url)


class CdnTests(unittest.TestCase):
    def test_short_reference_gets_confirmed_patient_prefix(self):
        self.assertEqual(
            content_path_from_reference("cdn://bc2ae09ec853459597a28cd1105e22dd"),
            "APS/Praxis/Patient/bc2ae09ec853459597a28cd1105e22dd",
        )

    def test_full_reference_is_not_prefixed_twice_and_whitespace_is_removed(self):
        self.assertEqual(
            content_path_from_reference(
                "  cdn://APS/Praxis/Patient/bc2ae09ec853459597a28cd1105e22dd  "
            ),
            "APS/Praxis/Patient/bc2ae09ec853459597a28cd1105e22dd",
        )

    def test_reference_is_encoded_and_file_is_written(self):
        http = FakeHttp([result(b"PDF", url="https://t2med.test")])
        client = CdnClient(http, "https://t2med.test/cdn/rest")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "doc.pdf"
            size = client.download("cdn://APS/Praxis/Patient/a b", target)
            self.assertEqual(size, 3)
            self.assertEqual(target.read_bytes(), b"PDF")
        self.assertTrue(http.calls[0][1].endswith("/delivery/APS/Praxis/Patient/a%20b"))

    def test_404_is_missing_cdn(self):
        client = CdnClient(
            FakeHttp([result(b"", status=404)]),
            "https://t2med.test/cdn/rest",
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MissingCdnError):
                client.download("cdn://APS/Praxis/Patient/missing", Path(tmp) / "doc")

    def test_content_path_requires_cdn_scheme(self):
        with self.assertRaises(Exception):
            content_path_from_reference("https://example.test/file")


class ApsTests(unittest.TestCase):
    def test_find_uses_confirmed_request_shape(self):
        dto = {
            "ref": {"objectId": {"id": "abc"}, "revision": 7},
            "text": "Alt",
            "verweis": "cdn://same",
            "gueltigkeitszeitpunkt": 123,
            "fachinformationstyp": 75,
            "kuerzel": "PDF",
        }
        http = FakeHttp([result({"successful": True, "dokumentverweisTO": dto})])
        snapshot = ApsClient(http, "https://t2med.test/aps/rest").find("abc", 7)
        self.assertEqual(snapshot.text, "Alt")
        request = http.calls[0][2]["json_body"]
        self.assertEqual(
            request,
            {
                "kontext": None,
                "dokumentverweisRef": {
                    "objectId": {"id": "abc"},
                    "revision": 7,
                },
            },
        )

    def test_update_returns_complete_dto_and_changes_only_text(self):
        dto = {
            "ref": {"objectId": {"id": "abc"}, "revision": 7},
            "text": "Alt",
            "verweis": "cdn://same",
            "gueltigkeitszeitpunkt": 123,
            "fachinformationstyp": 75,
            "kuerzel": "PDF",
            "unzugeordnet": False,
        }
        find_http = FakeHttp([result({"successful": True, "dokumentverweisTO": dto})])
        snapshot = ApsClient(find_http, "https://t2med.test/aps/rest").find("abc", 7)
        update_http = FakeHttp([result({"successful": True})])
        ApsClient(update_http, "https://t2med.test/aps/rest").update_text(snapshot, "Neu")
        body = update_http.calls[0][2]["json_body"]
        self.assertEqual(body["dokumentverweis"]["text"], "Neu")
        expected = dict(dto)
        expected["text"] = "Neu"
        self.assertEqual(body["dokumentverweis"], expected)
        self.assertFalse(body["neuerEintrag"])
        self.assertIsNone(body["uploadToken"])
        self.assertEqual(dto["text"], "Alt")


if __name__ == "__main__":
    unittest.main()
