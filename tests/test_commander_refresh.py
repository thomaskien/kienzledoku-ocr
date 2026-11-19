import importlib.util
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from kienzledoku_ocr_backfill.commander import CommanderCleaner, GAP
from kienzledoku_ocr_backfill.errors import OcrError
from kienzledoku_ocr_backfill.fhir_client import FhirError, SessionError
from kienzledoku_ocr_backfill.fhir_device import device_client
from test_fhir import BASE, response


class RefreshTests(unittest.TestCase):
    def login(self, *grants, expires=None, refresh=True):
        auth = Mock()
        first = {"access_token": "access1"}
        if refresh:
            first["refresh_token"] = "refresh1"
        if expires is not None:
            first["expires_in"] = expires
        auth.open.side_effect = [response({"verification_uri": "https://test.invalid/v", "user_code": "u",
                                           "device_code": "d", "interval": 1, "expires_in": 300}), response(first), *grants]
        self.log = Mock()
        with patch("kienzledoku_ocr_backfill.fhir_device.build_opener", return_value=auth), \
                patch("kienzledoku_ocr_backfill.fhir_device.getpass.getpass", return_value="secret"), \
                patch("kienzledoku_ocr_backfill.fhir_device.time.sleep"):
            client = device_client(BASE, "api-key", "app", ssl.create_default_context(), self.log)
        client.opener = Mock()
        return client, auth

    def test_refresh_rotation_for_fhir_and_cdn(self):
        client, auth = self.login(response({"access_token":"access2","refresh_token":"refresh2"}),
                                  response({"access_token":"access3"}))
        denied = lambda: HTTPError(BASE, 401, "expired", {}, None)
        client.opener.open.side_effect = [denied(), response({}), denied(), response({})]
        client.json(BASE + "/Patient")
        with client.open("https://test.invalid:16570/cdn/s", fhir=False):
            pass
        for i, tok in enumerate(("refresh1","refresh2"), start=2):
            req = auth.open.call_args_list[i].args[0]
            self.assertEqual(parse_qs(req.data.decode()), {"grant_type":["refresh_token"], "refresh_token":[tok], "client_id":["app"]})
            self.assertTrue(req.get_header("Authorization").startswith("Basic "))
        self.assertIsNone(client.opener.open.call_args.args[0].get_header("X-api-key"))
        self.assertNotIn("refresh1", repr(self.log.mock_calls))
        self.assertEqual(client.token, "access3")

    def test_expiry_before_write_and_no_repeat_after_transport_failure(self):
        client, auth = self.login(response({"access_token":"access2"}), expires=60)
        client.renew_at = 0
        client.opener.open.side_effect = OSError("lost response")
        with self.assertRaises(FhirError):
            client.json(BASE + "/Observation", body={"test": 1})
        self.assertEqual(auth.open.call_count, 3)
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertEqual(client.opener.open.call_args.args[0].get_header("Authorization"), "Bearer access2")

    def test_no_refresh_or_second_401_or_403_stops(self):
        for refresh, status, n in ((False,401,1),(True,401,2),(True,403,1)):
            with self.subTest(refresh=refresh,status=status):
                client, _ = self.login(response({"access_token":"access2"}), refresh=refresh)
                client.opener.open.side_effect = HTTPError(BASE, status, "rejected", {}, None)
                with self.assertRaises(SessionError):
                    client.json(BASE + "/Encounter/id")
                self.assertEqual(client.opener.open.call_count, n)

    def test_refresh_rejection_stops_before_any_create(self):
        client, auth = self.login(response({"error":"invalid_grant"},400), expires=60)
        client.renew_at = 0
        with self.assertRaises(SessionError):
            client.json(BASE + "/Observation",body={})
        client.opener.open.assert_not_called()
        self.assertEqual(auth.open.call_count, 3)


class CleaningTests(unittest.TestCase):
    def setUp(self):
        self.client = CommanderCleaner("http://127.0.0.1:8185",model="gruendlich",progress=lambda x: None)

    def result(self, value, finish="stop"):
        return {"choices":[{"message":{"content":value},"finish_reason":finish}]}

    def test_no_new_clinical_text_or_lost_numbers_negation(self):
        with patch.object(self.client, "_request", return_value=self.result('{"noise":[1,2,3,4]}')):
            actual = self.client.clean_page("Befund\n]||~]~\n5 mg\nkein Hinweis",1)
        self.assertEqual(actual, "Befund\n"+GAP+"\n5 mg\nkein Hinweis")

    def test_invented_text_bad_ids_and_truncation_cannot_be_written(self):
        for value, finish in [('{"noise":[100]}',"stop"),('{"noise":[true]}',"stop"),
                              ('{"noise":[],"text":"Metoprolol"}',"stop"), ('{"noise":[]}',"length")]:
            with patch.object(self.client, "_request", return_value=self.result(value,finish)), self.assertRaises(OcrError):
                self.client.clean_page("Fiktiver Befund",1)

    def test_chunks_preserve_order_and_contents(self):
        source = "\n".join("Fiktiver Befund" for _ in range(150)) + "\n" + "ü"*6001
        with patch.object(self.client, "_request", return_value=self.result('{"noise":[]}')) as req:
            self.assertEqual(self.client.clean_page(source,1),source)
            self.assertEqual(req.call_count,3)

    def test_bmp_replacement_skips_commander_in_mixed_pdf(self):
        from kienzledoku_ocr_backfill.ocr import OcrmypdfBackend
        from kienzledoku_ocr_backfill.medication_plans import MedicationPlanScan
        clean = Mock(return_value="bereinigt")
        backend = OcrmypdfBackend(clean_page=clean)
        pages = MedicationPlanScan({1:"BMP exakt"}, 2,{})
        with patch.object(backend,"_extract_page_text",return_value="Rohtext"):
            self.assertEqual(backend._mixed_text(Path("test.pdf"),pages),"BMP exakt\n\nbereinigt")
        clean.assert_called_once_with("Rohtext",2)

    def test_existing_desktop_preserves_config_and_arguments(self):
        spec=importlib.util.spec_from_file_location("handler",Path(__file__).resolve().parents[1]/"scripts/install-linux-handler.py")
        handler=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handler)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME":tmp, "XDG_DATA_HOME":tmp}), \
            patch.object(handler.sys,"platform","linux"), patch.object(handler.getpass,"getpass",return_value="test"), \
            patch.object(handler.subprocess,"run"), patch.object(handler,"load_tls"):
            handler.main(["--base-url",BASE,"--limit","4","--patient","a"])
            cfg=Path(tmp)/"kienzleocr/config.json"
            settings=json.loads(cfg.read_text())
            settings.update({"base_url": BASE + "/", "server_certificate":"/test/server.pem","client_id":"existing","unrelated":True})
            cfg.write_text(json.dumps(settings))
            desktop=Path(tmp)/"applications/kienzleocr.desktop"
            old=desktop.read_text()
            desktop.write_text(old.replace(str(handler.ROOT / "kienzledoku-ocr.py"), "/older directory/kienzledoku-ocr.py"))
            handler.main(["--update-existing","--commander-url","http://127.0.0.1:8185"])
            self.assertEqual(desktop.read_text(),old)
            self.assertEqual(json.loads(cfg.read_text()),dict(settings,commander_url="http://127.0.0.1:8185/v1"))
