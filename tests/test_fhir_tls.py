"""Real TLS regression tests; only ephemeral localhost servers and dummy tokens."""

from contextlib import contextmanager
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kienzledoku_ocr_backfill.fhir_client import Client, FhirError, SessionError
from kienzledoku_ocr_backfill.fhir_tls import TLSConfigurationError, connection_error, load_tls


class TLSRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="kienzle-tls-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)

        def openssl(*args):
            subprocess.run(["openssl", *args], cwd=cls.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # Reproduce the practice's self-signed certificate without a host/IP SAN.
        for name in ("trusted", "rogue"):
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=T2med Test Server", "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign,digitalSignature",
                    "-addext", "subjectKeyIdentifier=hash", "-keyout", name + ".key", "-out", name + ".pem")
        # Even a different certificate signed by the trusted key must fail pinning.
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                "-keyout", "child.key", "-out", "child.csr")
        (cls.root / "extensions.cnf").write_text("subjectAltName=DNS:localhost\nbasicConstraints=CA:FALSE\n"
                                                 "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
        openssl("x509", "-req", "-in", "child.csr", "-CA", "trusted.pem", "-CAkey", "trusted.key",
                "-CAcreateserial", "-days", "1", "-extfile", "extensions.cnf", "-out", "child.pem")

    def tls(self):
        return load_tls({"server_certificate": str(self.root / "trusted.pem")})

    @contextmanager
    def server(self, certificate):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                data = b'{"resourceType":"Bundle","type":"searchset","total":0}'
                self.send_response(200)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.root / (certificate + ".pem"), self.root / (certificate + ".key"))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield f"https://localhost:{server.server_port}/aps/fhir/api/r4", received
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_pinned_self_signed_certificate_without_san_works(self):
        tls = self.tls()
        self.assertEqual(tls.context.verify_mode, ssl.CERT_REQUIRED)
        with self.server("trusted") as (base, received):
            self.assertEqual(Client(base, "dummy-token", "dummy-key", tls).patients(), {})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["Authorization"], "Bearer dummy-token")
        self.assertEqual(received[0]["X-Api-Key"], "dummy-key")

    def test_untrusted_replacement_rejected_before_credentials(self):
        with self.server("rogue") as (base, received):
            with self.assertRaisesRegex(FhirError, "TLS-Zertifikatsprüfung"):
                Client(base, "dummy-token", "dummy-key", self.tls()).patients()
        self.assertEqual(received, [])

    def test_different_certificate_signed_by_trusted_key_is_rejected(self):
        with self.server("child") as (base, received):
            with self.assertRaisesRegex(FhirError, "Serverzertifikat geändert"):
                Client(base, "dummy-token", "dummy-key", self.tls()).patients()
        self.assertEqual(received, [])

    def test_standard_ca_validation_still_checks_hostname(self):
        tls = load_tls({"ca_file": str(self.root / "trusted.pem")})
        self.assertTrue(tls.context.check_hostname)
        with self.server("trusted") as (base, received):
            with self.assertRaisesRegex(FhirError, "TLS-Zertifikatsprüfung"):
                Client(base, "dummy-token", "dummy-key", tls).patients()
        self.assertEqual(received, [])

    def test_standard_ca_validation_accepts_valid_hostname(self):
        tls = load_tls({"ca_file": str(self.root / "trusted.pem")})
        with self.server("child") as (base, received):
            self.assertEqual(Client(base, "dummy-token", "dummy-key", tls).patients(), {})
        self.assertEqual(len(received), 1)

    def test_default_validation_does_not_trust_self_signed_certificate(self):
        with self.server("trusted") as (base, received):
            with self.assertRaisesRegex(FhirError, "TLS-Zertifikatsprüfung"):
                Client(base, "dummy-token", "dummy-key", load_tls({})).patients()
        self.assertEqual(received, [])

    def test_missing_certificate_has_safe_actionable_error(self):
        with self.assertRaisesRegex(TLSConfigurationError, "Zertifikatsdatei fehlt"):
            load_tls({"server_certificate": str(self.root / "missing-secret.pem")})

    def test_conflicting_trust_settings_rejected(self):
        with self.assertRaisesRegex(TLSConfigurationError, "Nur ca_file oder server_certificate"):
            load_tls({"server_certificate": "a", "ca_file": "b"})

    def test_multiple_server_certificates_rejected(self):
        pem = (self.root / "trusted.pem").read_text() + (self.root / "rogue.pem").read_text()
        bundle = self.root / "bundle.pem"
        bundle.write_text(pem)
        with self.assertRaises(TLSConfigurationError):
            load_tls({"server_certificate": str(bundle)})

    def test_network_errors_are_actionable_without_sensitive_exception_text(self):
        cases = [
            (socket.gaierror(-2, "SECRET"), "DNS"),
            (TimeoutError("SECRET"), "Zeitüberschreitung"),
            (ConnectionRefusedError("SECRET"), "abgelehnt"),
            (OSError(errno.ENETUNREACH, "SECRET"), "VPN"),
            (ssl.SSLError("SECRET"), "HTTPS-Port"),
            (OSError("SECRET"), "Netzwerk"),
        ]
        client = Client("https://localhost/aps/fhir/api/r4", "SECRET-TOKEN", "SECRET-KEY", load_tls({}))
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                for wrapped in (error, URLError(error)):
                    message = connection_error(wrapped)
                    self.assertIn(expected, message)
                    self.assertNotIn("SECRET", message)
                with patch.object(client.opener, "open", side_effect=URLError(error)):
                    with self.assertRaises(FhirError) as caught:
                        client.patients()
                    self.assertIn(expected, str(caught.exception))
                    self.assertNotIn("SECRET", str(caught.exception))

    def test_http_authentication_errors_keep_session_semantics(self):
        client = Client("https://localhost/aps/fhir/api/r4", "dummy-token", "dummy-key", load_tls({}))
        for status in (401, 403):
            with self.subTest(status=status):
                error = HTTPError(client.base + "/Patient", status, "SECRET", {}, io.BytesIO(b"SECRET"))
                with patch.object(client.opener, "open", side_effect=error):
                    with self.assertRaises(SessionError) as caught:
                        client.patients()
                    self.assertEqual(caught.exception.status, status)
                    self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
