import base64
from html import escape
import importlib.util
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit

from kienzledoku_ocr_backfill.fhir_backfill import Backfill, original_date
from kienzledoku_ocr_backfill.fhir_cli import parse_link
from kienzledoku_ocr_backfill.fhir_client import Client, FhirError, SessionError
from kienzledoku_ocr_backfill.fhir_device import device_client
from kienzledoku_ocr_backfill.journal import Journal


BASE = "https://test.invalid:16567/aps/fhir/api/r4"
DATE = "2022-06-15T12:34:56.123+02:00"
PDF = b"%PDF-1.4 synthetic"
OUTCOME = {"resourceType": "OperationOutcome", "issue": [{"severity": "information"}]}


def response(data, status=200, location=""):
    r = io.BytesIO(data if isinstance(data, bytes) else json.dumps(data).encode())
    r.status = status
    r.headers = {"Location": location}
    return r


def item(resource):
    return {"resource": resource}


class Server:
    def __init__(self):
        self.encounters = {"start": "Patient/a"}
        self.notes = {"a": [], "b": []}
        self.requests = []
        self.lost = self.misbound = self.hide_notes = False
        self.next = BASE + "?_getpages=second"
        self.total = 2

    def open(self, req, timeout):
        path = urlsplit(req.full_url).path
        method = req.get_method()
        self.requests.append((method, req.full_url))
        if path.endswith("/Patient") or "_getpages" in req.full_url:
            first = path.endswith("/Patient")
            return response({"resourceType": "Bundle", "type": "searchset", "total": self.total,
                             "entry": [item({"resourceType": "Patient", "id": "a" if first else "b"})],
                             "link": [{"relation": "next", "url": self.next}] if first else []})
        if path.endswith("/$eakte"):
            pid = path.split("/")[-2]
            doc = {"resourceType": "Composition", "subject": {"reference": "Patient/" + pid},
                   "date": DATE, "section": [{"entry": [{"reference": "DocumentReference/d" + pid}]}]}
            notes = [item({"resourceType": "Composition", "subject": doc["subject"], "date": n["effectiveDateTime"],
                           "section": [{"text": {"div": "<div>" + escape(n["valueString"]) + "</div>"}}]})
                     for n in self.notes[pid] if not self.hide_notes]
            return response({"resourceType": "Bundle", "entry": [item(doc), *notes]})
        if "/DocumentReference/" in path:
            pid = path[-1]
            attachment = ({"data": base64.b64encode(PDF).decode()} if pid == "a"
                          else {"url": "/cdn/rest/delivery/file"})
            return response({"resourceType": "DocumentReference", "id": "d" + pid,
                             "subject": {"reference": "Patient/" + pid}, "date": "2025-01-01",
                             "content": [{"attachment": {**attachment, "contentType": "application/pdf"}}]})
        if path.startswith("/cdn/"):
            assert req.get_header("X-api-key") is None
            return response(PDF)
        if path.endswith("/Encounter"):
            data = json.loads(req.data)
            self.encounters["other"] = data["subject"]["reference"]
            return response(OUTCOME, 201, BASE + "/Encounter/other/_history/0")
        if "/Encounter/" in path:
            identifier = path.rsplit("/", 1)[-1]
            return response({"resourceType": "Encounter", "id": identifier,
                             "subject": {"reference": "Patient/a" if self.misbound else self.encounters[identifier]},
                             "participant": [{"individual": {"reference": "Practitioner/role"}}],
                             "serviceProvider": {"reference": "Organization/place"}})
        if path.endswith("/Observation") and method == "POST":
            note = json.loads(req.data)
            pid = self.encounters[note["identifier"][0]["value"]].split("/")[-1]
            self.notes[pid].append(note)
            if self.lost:
                raise OSError()
            return response(OUTCOME, 201)
        raise AssertionError(method + " " + path)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "journal"
        self.client = Client(BASE, "synthetic-token", "synthetic-key", ssl.create_default_context())
        self.client.opener = self.server = Server()
        self.ocr = Mock()
        self.ocr.extract_text.return_value = "Befundtext\nA & B"

    def batch(self, apply=False, pid=None):
        with Journal(self.path) as journal:
            run = Backfill(self.client, journal, self.ocr, lambda _: None, apply=apply)
            run.process("start", patient_filter=pid)
            return run.counts

    def test_failed_commander_stops_only_that_document_before_post(self):
        from kienzledoku_ocr_backfill.commander import CommanderError

        self.ocr.extract_text.side_effect = [CommanderError("Antwort ungültig"), "zweites Dokument"]
        self.assertEqual(self.batch(apply=True), {"document_failed": 1, "created_verified": 1})
        self.assertEqual(len(self.server.notes["a"]), 0)
        self.assertEqual(len(self.server.notes["b"]), 1)

    def test_all_patients_dry_run_only_gets(self):
        self.assertEqual(self.batch(), {"dry_run": 2})
        self.assertEqual({m for m, u in self.server.requests}, {"GET"})
        self.assertEqual(self.ocr.extract_text.call_count, 2)

    def test_patient_scoped_creates_and_remote_dedup_even_with_new_journal(self):
        self.assertEqual(self.batch(True), {"created_verified": 2})
        for pid in ("a", "b"):
            note, = self.server.notes[pid]
            self.assertEqual(note["effectiveDateTime"], DATE)
            self.assertEqual(note["extension"][0]["valueString"], "OCR")
            self.assertIn("Befundtext\nA & B", note["valueString"])
        self.path = self.path.with_name("second-journal")
        self.assertEqual(self.batch(True), {"already_ocr": 2})
        self.assertEqual(self.ocr.extract_text.call_count, 2)

    def test_wrong_patient_context_is_fatal_before_text_post(self):
        self.server.misbound = True
        with self.assertRaises(SessionError):
            self.batch(True, "b")
        self.assertEqual(self.server.notes, {"a": [], "b": []})

    def test_device_session_creates_its_own_context_even_for_start_patient(self):
        with Journal(self.path) as journal:
            run = Backfill(self.client, journal, self.ocr, lambda _: None, apply=True)
            run.process("start", patient_filter="a", login=lambda: self.client)
        self.assertEqual(run.counts, {"created_verified": 1})
        self.assertEqual(self.server.notes["a"][0]["identifier"][0]["value"], "other")

    def test_lost_post_response_immediately_verified(self):
        self.server.lost = True
        self.assertEqual(self.batch(True, "a"), {"created_verified": 1})
        self.assertEqual(len(self.server.notes["a"]), 1)

    def test_missing_context_after_ocr_recreated_and_validated(self):
        old = self.server.open
        seen = 0

        def expire(req, timeout):
            nonlocal seen
            if req.full_url.endswith("/Encounter/start"):
                seen += 1
                if seen > 1:
                    raise HTTPError(req.full_url, 410, "Gone", {}, None)
            return old(req, timeout)
        with patch.object(self.server, "open", side_effect=expire):
            self.assertEqual(self.batch(True, "a"), {"created_verified": 1})
        self.assertEqual(self.server.notes["a"][0]["identifier"][0]["value"], "other")

    def test_session_loss_after_ocr_resumes_prepared_text(self):
        old = self.server.open
        def expire(req, timeout):
            if req.full_url.endswith("/$eakte") and self.ocr.extract_text.called:
                raise HTTPError(req.full_url, 401, "Expired", {}, None)
            return old(req, timeout)
        with patch.object(self.server, "open", side_effect=expire), self.assertRaises(SessionError):
            self.batch(True, "a")
        saved, = self.path.parent.glob("journal.pending/*/*")
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.batch(True, "a"), {"created_verified": 1})
        self.assertEqual(self.ocr.extract_text.call_count, 1)
        self.assertFalse(saved.exists())

    def test_unconfirmed_create_never_reposted(self):
        self.server.hide_notes = True
        self.server.lost = True
        self.assertEqual(self.batch(True, "a"), {"unconfirmed": 1})
        notes = self.server.notes["a"][:]
        self.server.notes["a"].clear()
        self.assertEqual(self.batch(True, "a"), {"unconfirmed": 1})
        self.server.notes["a"] = notes
        self.server.hide_notes = False
        self.assertEqual(self.batch(True, "a"), {"already_ocr": 1})
        self.assertEqual(len([u for m,u in self.server.requests if u.endswith("/Observation")]), 1)
        self.assertNotIn("synthetic-token", self.path.read_text())
        self.assertNotIn("Befundtext", self.path.read_text())

    def test_legacy_marker_skips_download_and_ocr(self):
        with patch("kienzledoku_ocr_backfill.fhir_backfill.parse_document",
                   return_value=[{"index": 0, "declaredMime": "application/pdf", "originalTitle": "Test",
                                  "entryText": "Titel\nkienzledoku OCR v1.00, 03.02.2025"}]):
            self.assertEqual(self.batch(True), {"already_ocr": 2})
        self.ocr.extract_text.assert_not_called()
        self.assertFalse(any(m == "POST" for m,u in self.server.requests))

    def test_inventory_incomplete_or_redirected_stops_before_creates(self):
        self.server.total = 3
        with self.assertRaises(FhirError):
            self.batch(True)
        self.server.next = "https://foreign.invalid/Patient"
        with self.assertRaises(FhirError):
            self.batch(True)
        self.assertFalse(any(m == "POST" or "foreign.invalid" in u for m,u in self.server.requests))

    def test_dates_fail_closed(self):
        for candidates in ([], [("Composition.date", "bad")],
                           [("Composition.date", DATE), ("Composition.date", "2023-01-01")]):
            with self.assertRaises(FhirError):
                original_date({"dateCandidates": candidates})

    def test_bad_document_does_not_skip_next_patient(self):
        opened = self.server.open

        def broken(req, timeout):
            if req.full_url == BASE + "/DocumentReference/da":
                return response({"resourceType": "DocumentReference", "id": "other"})
            return opened(req, timeout)

        with patch.object(self.server, "open", side_effect=broken):
            self.assertEqual(self.batch(True), {"document_failed": 1, "created_verified": 1})
        self.assertEqual(self.server.notes["a"], [])

    def test_permission_denied_for_one_document_does_not_stop_batch(self):
        opened = self.server.open

        def restricted(req, timeout):
            if req.full_url == BASE + "/DocumentReference/da":
                raise HTTPError(req.full_url, 403, "Forbidden", {}, None)
            return opened(req, timeout)

        with patch.object(self.server, "open", side_effect=restricted):
            self.assertEqual(self.batch(True), {"document_failed": 1, "created_verified": 1})
        self.assertEqual(self.server.notes["a"], [])

    def test_device_authorization_uses_trusted_host_and_polls(self):
        opener = Mock()
        opener.open.side_effect = [response({"verification_uri": "https://test.invalid:16596/verify",
                                             "user_code": "TEST", "device_code": "code",
                                             "interval": 1, "expires_in": 100}),
                                   response({"error": "authorization_pending"}, 400),
                                   response({"error": "slow_down"}, 400),
                                   response({"access_token": "device-token"})]
        report = Mock()
        with patch("kienzledoku_ocr_backfill.fhir_device.build_opener", return_value=opener), \
                patch("kienzledoku_ocr_backfill.fhir_device.getpass.getpass", return_value="device-secret"), \
                patch("kienzledoku_ocr_backfill.fhir_device.time.sleep") as sleep:
            client = device_client(BASE, "private-key", "app", ssl.create_default_context(), report)
        self.assertEqual(client.token, "device-token")
        self.assertNotIn("device-secret", str(report.call_args))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 1, 6])
        for c in opener.open.call_args_list:
            self.assertTrue(c.args[0].full_url.startswith("https://test.invalid:16596/oauth2/"))
            self.assertNotIn(b"device-secret", c.args[0].data)

    def test_link_and_handler(self):
        params = {"kontextId": "test", "oAuthToken": "synthetic-test", "fhirBasisUrl": BASE}
        uri = "kienzleocr://start?" + urlencode(params)
        self.assertEqual(parse_link(uri, BASE), ("test", "synthetic-test"))
        for invalid in (uri + "&apply=true", uri + "&kontextId=foo", uri.replace("test.invalid", "other.invalid")):
            with self.assertRaises(FhirError):
                parse_link(invalid, BASE)
        script = Path(__file__).resolve().parents[1] / "scripts/install-linux-handler.py"
        spec = importlib.util.spec_from_file_location("handler", script)
        handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handler)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "XDG_DATA_HOME": self.tmp.name}), \
                patch.object(handler.sys, "platform", "linux"), patch.object(handler.getpass, "getpass", return_value="key"), \
                patch.object(handler.subprocess, "run") as register:
            handler.main(["--base-url", BASE])
            desktop = Path(self.tmp.name) / "applications/kienzleocr.desktop"
            self.assertIn(" -- %u", desktop.read_text())
            self.assertNotIn("--apply", desktop.read_text())
            handler.main(["--apply", "--limit", "1"])
            self.assertIn('"--apply"', desktop.read_text())
            self.assertEqual((Path(self.tmp.name) / "kienzleocr/config.json").stat().st_mode & 0o777, 0o600)
            register.assert_called_with(["xdg-mime", "default", desktop.name, "x-scheme-handler/kienzleocr"], check=True)
            profile = {"url": "https://amdb.invalid", "token": "amdb-test-token", "ca_pem": "test-cert"}
            config = Path(self.tmp.name) / "kienzleocr/config.json"
            with patch.object(handler, "load_amdb_profile", return_value=profile):
                handler.main(["--amdb-profile", "/private/profile.json"])
                handler.main(["--limit", "1"])
                self.assertEqual(json.loads(config.read_text())["amdb_remote"], profile)
                self.assertNotIn(profile["token"], desktop.read_text())
                handler.main(["--local-amdb"])
                self.assertNotIn("amdb_remote", json.loads(config.read_text()))
                handler.main(["--amdb-profile", "/private/profile.json"])
                handler.main(["--base-url", BASE.replace("test.invalid", "other.invalid")])
                self.assertNotIn("amdb_remote", json.loads(config.read_text()))
