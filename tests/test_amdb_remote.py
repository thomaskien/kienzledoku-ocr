import io
import json
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from wsgiref.simple_server import make_server, WSGIRequestHandler

from kienzledoku_ocr_backfill.amdb_remote import RemoteAmdbResolver, load_amdb_profile
from kienzledoku_ocr_backfill.amdb_service import create_app
from kienzledoku_ocr_backfill.errors import OcrError
from kienzledoku_ocr_backfill.medication_plans import MedicationPlanScanner
from kienzledoku_ocr_backfill.qr_extractor import QrCode, QrExtractionResult
from kienzledoku_ocr_backfill.t2med_amdb import T2medAmdbError


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class ServerRequestTests(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)
        self.factory = Mock()
        self.app = create_app(self.token, self.factory)

    def post(self, obj, authorized=True):
        body = json.dumps(obj).encode()
        env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/lookup", "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)}
        if authorized:
            env["HTTP_AUTHORIZATION"] = "Bearer " + self.token
        status = Mock()
        result = json.loads(self.app(env, status)[0])
        return status.call_args.args[0], result

    def test_authentication_and_exact_pzn_required_before_opening_database(self):
        self.assertEqual(self.post({"pzn": "09322739"}, False)[0], "401 Unauthorized")
        for obj in (None, {}, {"pzn": "１２３４５６７８"}, {"pzn": "09322739", "patient": "synthetic"},
                    {"pzn": "9322739"}, {"pzn": 9322739}):
            self.assertEqual(self.post(obj)[0], "400 Bad Request")
        self.factory.assert_not_called()

    def test_unknown_and_unavailable_are_distinct(self):
        self.factory.return_value.lookup.return_value = None
        self.assertEqual(self.post({"pzn": "12345678"}), ("200 OK", {"drug": None}))
        self.factory.return_value.lookup.side_effect = T2medAmdbError("internal")
        self.assertEqual(self.post({"pzn": "12345678"}), ("503 Service Unavailable", None))
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.factory.return_value.close.call_count, 2)


class RemoteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl für TLS-Test erforderlich")
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        root = Path(temp.name)
        cert, key = root / "server.crt", root / "server.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                        "-days", "1", "-subj", "/CN=test", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-addext", "basicConstraints=critical,CA:FALSE", "-addext", "extendedKeyUsage=serverAuth",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        cls.token = secrets.token_urlsafe(32)
        cls.database = Mock()
        cls.factory = Mock(return_value=cls.database)
        server = make_server("127.0.0.1", 0, create_app(cls.token, cls.factory), handler_class=QuietHandler)
        cls.addClassCleanup(server.server_close)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(thread.join)
        cls.addClassCleanup(server.shutdown)
        cls.profile = {"url": f"https://127.0.0.1:{server.server_port}", "token": cls.token, "ca_pem": cert.read_text()}

    def setUp(self):
        self.database.reset_mock(side_effect=True)
        self.database.metadata.return_value = {"schema": "mmidata2", "serverVersion": "test",
                                              "sourceTable": "MEDPLAN_PACKAGE"}
        self.database.lookup.return_value = {"pzn": "09322739", "name": "Testpräparat", "form_long": "Tabl",
                                            "form_short": "TAB", "substances": [{"name": "Teststoff", "strength": "1 mg"}]}

    def scan(self, xml, profile=None):
        code = QrCode(page=1, code_type="DataMatrix", data=xml.encode(), text=xml,
                      rect={}, polygon=(), retry=False, dpi=300)
        found = QrExtractionResult("example.pdf", (code,), (), 2)
        with patch("kienzledoku_ocr_backfill.medication_plans.extract_qr_codes", return_value=found):
            return MedicationPlanScanner(amdb_remote=self.profile if profile is None else profile).scan(Path("example.pdf"))

    def test_medication_page_over_tls_and_document_local_cache(self):
        scan = self.scan('<MP v="027"><P g="Testperson"/><S><M p="9322739" m="1"/><M p="9322739"/></S></MP>')
        self.assertEqual(set(scan.pages), {1})
        self.assertIn("BUNDESMEDIKATIONSPLAN für Testperson", scan.pages[1])
        self.assertIn("Testpräparat (Teststoff)", scan.pages[1])
        self.assertIn("1 mg, Tabl // PZN 09322739", scan.pages[1])
        self.assertIn("Einnahme: 1-0-0-0", scan.pages[1])
        self.assertEqual(scan.diagnostics["amdbSchema"], "mmidata2")
        self.database.lookup.assert_called_once_with("09322739")
        self.assertNotIn(self.token, repr(scan.diagnostics))

    def test_database_outage_blocks_document_and_unknown_pzn_keeps_number(self):
        self.database.lookup.side_effect = T2medAmdbError("database down")
        xml = '<MP v="027"><S><M p="9531845"/></S></MP>'
        with self.assertRaisesRegex(OcrError, "503"):
            self.scan(xml)
        self.database.lookup.side_effect = None
        self.database.lookup.return_value = None
        scan = self.scan(xml)
        self.assertIn("PZN 09531845", scan.pages[1])
        self.assertEqual(scan.diagnostics["plans"][0]["unresolvedPzns"], ["09531845"])

    def test_no_network_for_free_text_and_non_medication_code(self):
        for xml in ('<MP v="027"><S><X t="Testkommentar"/></S></MP>', 'unrelated-data'):
            self.scan(xml, {})
        self.database.metadata.assert_not_called()
        self.database.lookup.assert_not_called()

    def test_wrong_pzn_and_bad_credentials_rejected(self):
        with self.assertRaisesRegex(T2medAmdbError, "09531845"):
            RemoteAmdbResolver(self.profile).lookup("09531845")
        with self.assertRaisesRegex(T2medAmdbError, "401"):
            RemoteAmdbResolver(dict(self.profile, token=secrets.token_urlsafe(32))).metadata()
        xml = '<MP v="027"><S><M p="9531845"/></S></MP>'
        with self.assertRaisesRegex(OcrError, "401"):
            self.scan(xml, dict(self.profile, token=secrets.token_urlsafe(32)))

    def test_profile_permissions_and_certificate_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(self.profile))
            path.chmod(0o600)
            self.assertEqual(load_amdb_profile(path), self.profile)
            path.chmod(0o644)
            with self.assertRaisesRegex(T2medAmdbError, "chmod 600"):
                load_amdb_profile(path)
        wrong = self.profile["url"].replace("127.0.0.1", "localhost")
        with self.assertRaisesRegex(T2medAmdbError, "Zertifikatsprüfung"):
            RemoteAmdbResolver(dict(self.profile, url=wrong)).metadata()
