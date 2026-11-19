"""TLS validation for a practice CA or an explicitly trusted T2med certificate."""

from dataclasses import dataclass
import errno
from functools import partial
import hashlib
import hmac
import http.client
from pathlib import Path
import socket
import ssl
from urllib.error import URLError
from urllib.request import HTTPSHandler


class TLSConfigurationError(RuntimeError):
    pass


class CertificateMismatch(ssl.SSLError):
    pass


@dataclass(frozen=True)
class TLSConfig:
    context: ssl.SSLContext
    certificate_sha256: bytes | None = None


def load_tls(config):
    certificate = config.get("server_certificate")
    ca_file = config.get("ca_file")
    if certificate and ca_file:
        raise TLSConfigurationError("Nur ca_file oder server_certificate konfigurieren.")
    try:
        if not certificate:
            return TLSConfig(ssl.create_default_context(cafile=ca_file or None))
        pem = Path(certificate).read_text(encoding="ascii").strip()
        if pem.count("-----BEGIN CERTIFICATE-----") != 1 or not pem.endswith("-----END CERTIFICATE-----"):
            raise ValueError("Expected exactly one server certificate")
        der = ssl.PEM_cert_to_DER_cert(pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # T2med's self-signed certificate has no host/IP SAN. Its exact, previously
        # trusted SHA-256 identity is checked before sending any HTTP headers.
        # Chain, signature and validity checks remain enabled; no system CAs are
        # added to this dedicated trust store.
        context.check_hostname = False
        context.load_verify_locations(cadata=pem)
        return TLSConfig(context, hashlib.sha256(der).digest())
    except (OSError, ValueError, UnicodeError):
        raise TLSConfigurationError(
            "TLS-Zertifikatsdatei fehlt oder ist ungültig. ca_file bzw. server_certificate prüfen."
        ) from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, certificate_sha256, **kwargs):
        self.certificate_sha256 = certificate_sha256
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        try:
            actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).digest()
            if not hmac.compare_digest(actual, self.certificate_sha256):
                raise CertificateMismatch("T2med-Zertifikat stimmt nicht mit dem hinterlegten Zertifikat überein.")
        except BaseException:
            self.close()
            raise


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, tls):
        super().__init__(context=tls.context)
        self.certificate_sha256 = tls.certificate_sha256

    def https_open(self, req):
        connection = partial(PinnedHTTPSConnection, certificate_sha256=self.certificate_sha256)
        return self.do_open(connection, req, context=self._context)


def https_handler(tls):
    # Retain support for callers supplying an ordinary SSLContext.
    if isinstance(tls, ssl.SSLContext):
        return HTTPSHandler(context=tls)
    if tls.certificate_sha256 is not None:
        return PinnedHTTPSHandler(tls)
    return HTTPSHandler(context=tls.context)


def connection_error(exc):
    """Actionable diagnostics without URLs, credentials or arbitrary server text."""
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, CertificateMismatch):
        return "T2med-Serverzertifikat geändert. Fingerabdruck im T2med-Client prüfen und Zertifikat neu hinterlegen."
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ("TLS-Zertifikatsprüfung fehlgeschlagen. Bei einer Praxis-CA --ca-file verwenden; "
                "bei einem selbstsignierten T2med-Zertifikat dessen Fingerabdruck im T2med-Client "
                "abgleichen und --server-certificate einrichten. Auch Gültigkeit und Servername prüfen.")
    if isinstance(reason, ssl.SSLError):
        return "TLS-Verbindung zu T2med fehlgeschlagen. HTTPS-Port und TLS-Konfiguration prüfen."
    if isinstance(reason, socket.gaierror):
        return "T2med-Servername konnte nicht aufgelöst werden. FHIR-Adresse und DNS prüfen."
    if isinstance(reason, TimeoutError):
        return "Zeitüberschreitung bei der T2med-Verbindung. Server, Netzwerk und Firewall prüfen."
    if isinstance(reason, ConnectionRefusedError):
        return "T2med hat die Verbindung abgelehnt. Serverdienst und Port der FHIR-Adresse prüfen."
    if isinstance(reason, OSError) and reason.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return "T2med-Server nicht erreichbar. Netzwerk und VPN-Verbindung prüfen."
    return "T2med-Verbindung unterbrochen oder fehlgeschlagen. Server und Netzwerk prüfen."
