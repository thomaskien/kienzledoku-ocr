"""T2med R4 client for the Asterisk patient inventory and Befundwand file path."""

import base64
import hashlib
import http.client
import io
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .fhir_model import valid_id
from .fhir_tls import connection_error, https_handler

PROFILE = "https://fhir.t2med.de/StructureDefinition/FhirApi"
MAX_JSON = 32 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024


class FhirError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class SessionError(FhirError):
    """The credential or patient binding no longer permits this run."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def safe_url(url):
    parsed = urlsplit(url)
    path = unquote(unquote(parsed.path))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or "\\" in path
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(c) <= 32 for c in url) or any(ord(c) < 32 for c in path)):
        raise FhirError("Unsichere T2med-Adresse.")
    return parsed


def check_outcome(data):
    if data.get("resourceType") != "OperationOutcome":
        raise FhirError("Erwartetes FHIR-OperationOutcome fehlt.")
    issues = data.get("issue")
    if not isinstance(issues, list) or not issues or any(
            not isinstance(i, dict) or i.get("severity") != "information" for i in issues):
        raise FhirError("FHIR hat die Anlage nicht eindeutig bestätigt.")


def patient_matches(entries):
    for entry in entries:
        patient = entry["resource"]
        mode = entry.get("search", {}).get("mode", "match")
        if patient.get("resourceType") == "OperationOutcome":
            check_outcome(patient)
        elif mode == "include":
            continue
        elif mode == "match" and patient.get("resourceType") == "Patient":
            yield entry
        else:
            raise FhirError("Unerwarteter Eintrag in der Patientensuche.")


class Client:
    def __init__(self, base, token, key, tls):
        self.base = base.rstrip("/")
        self.origin = safe_url(self.base)
        if self.origin.path != "/aps/fhir/api/r4" or self.origin.query:
            raise FhirError("Ungültige FHIR-Basisadresse.")
        if not all(isinstance(v, str) and v and v.isascii() and v.isprintable()
                   for v in (token, key)):
            raise FhirError("OAuth-Token oder API-Key fehlt oder ist ungültig.")
        self.token, self.key = token, key
        self.refresh = None
        self.renew_at = None
        self.opener = build_opener(ProxyHandler({}), https_handler(tls), NoRedirect())

    def set_token_lifetime(self, seconds):
        self.renew_at = None
        if isinstance(seconds, (int, str)) and re.fullmatch(r"[0-9]{1,8}", str(seconds)) and int(seconds) > 0:
            seconds = int(seconds)
            self.renew_at = time.monotonic() + seconds - min(60, seconds / 5)

    def renew_token(self):
        try:
            self.token, lifetime = self.refresh()
        except (OSError, http.client.HTTPException, ValueError, SessionError):
            raise SessionError("FHIR-Token konnte nicht erneuert werden. Geräteanmeldung wiederholen.", status=401) from None
        self.set_token_lifetime(lifetime)

    def open(self, url, *, fhir=True, body=None, patient_profile=False):
        parsed = safe_url(url)
        if fhir:
            allowed = ((parsed.hostname, parsed.port or 443) ==
                       (self.origin.hostname, self.origin.port or 443) and
                       (parsed.path == self.origin.path or parsed.path.startswith(self.origin.path + "/")))
        else:
            allowed = (parsed.hostname == self.origin.hostname and parsed.port == 16570
                       and parsed.path.startswith("/cdn/") and body is None)
        if not allowed:
            raise FhirError("Verweis liegt außerhalb des freigegebenen T2med-Dienstes.")
        if self.refresh and self.renew_at is not None and time.monotonic() >= self.renew_at:
            self.renew_token()
        headers = {"Authorization": "Bearer " + self.token, "Accept-Encoding": "identity"}
        if fhir:
            headers.update({"X-API-Key": self.key, "Accept": "application/fhir+json",
                            "X-TreatWarningAsError": "true"})
        if patient_profile:
            headers["X-FHIR-Profile"] = PROFILE + "Patient|1.0.0"
        if body is not None:
            headers.update({"Content-Type": "application/fhir+json", "Prefer": "return=OperationOutcome"})
        request = Request(url, headers=headers, data=json.dumps(body).encode() if body is not None else None)
        for attempt in range(2):
            try:
                return self.opener.open(request, timeout=40)
            except HTTPError as exc:
                exc.close()
                if exc.code == 401 and attempt == 0 and self.refresh:
                    # An explicit authentication rejection precedes execution on T2med.
                    self.renew_token()
                    request.add_header("Authorization", "Bearer " + self.token)
                    continue
                if exc.code == 401 or (exc.code == 403 and (body is not None or patient_profile or "/Encounter/" in parsed.path)):
                    raise SessionError(f"T2med HTTP {exc.code}. Zugriff prüfen und frisch aus T2med starten.", status=exc.code) from None
                raise FhirError(f"T2med HTTP {exc.code}.", status=exc.code) from None
            except (OSError, http.client.HTTPException) as exc:
                raise FhirError(connection_error(exc)) from None

    def json(self, url, *, body=None, patient_profile=False):
        with self.open(url, body=body, patient_profile=patient_profile) as response:
            try:
                raw = response.read(MAX_JSON + 1)
            except (OSError, http.client.HTTPException):
                raise FhirError("FHIR-Antwort unterbrochen.") from None
            if len(raw) > MAX_JSON:
                raise FhirError("FHIR-Antwort ist zu groß.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise FhirError("Ungültige FHIR-JSON-Antwort.")
            if body is None and response.status != 200:
                raise FhirError("Unvollständige FHIR-Antwort.")
            return data, response.status, response.headers.get("Location", "")

    def pages(self, path, *, inventory=False):
        url = initial = self.base + "/" + path
        seen, count, total = set(), 0, None
        while url:
            if url in seen or len(seen) >= 10000:
                raise FhirError("Unvollständige oder zyklische FHIR-Seitenfolge.")
            seen.add(url)
            bundle, _, _ = self.json(url, patient_profile=inventory)
            if (bundle.get("resourceType") != "Bundle" or not isinstance(bundle.get("entry", []), list)
                    or (inventory and bundle.get("type") != "searchset")):
                raise FhirError("Ungültiges FHIR-Ergebnisbundle.")
            if "total" in bundle:
                value = bundle["total"]
                if type(value) is not int or value < 0 or total not in (None, value):
                    raise FhirError("Widersprüchliche FHIR-Gesamtzahl.")
                total = value
            if inventory:
                bundle["entry"] = list(patient_matches(bundle.get("entry", [])))
            count += len(bundle.get("entry", []))
            yield bundle
            links = [l.get("url") for l in bundle.get("link", []) if l.get("relation") == "next"]
            if not links:
                break
            if len(links) != 1 or not isinstance(links[0], str) or not links[0]:
                raise FhirError("Ungültiger FHIR-Folgelink.")
            url = urljoin(url, links[0])
            parsed = safe_url(url)
            query = parse_qs(parsed.query)
            if (parsed.netloc != self.origin.netloc
                    or not (parsed.path == urlsplit(initial).path or
                            (inventory and parsed.path.rstrip("/") == self.origin.path and query.get("_getpages")))
                    or any(re.search(r"token|password|secret|authorization|key", k, re.I) for k in query)):
                raise FhirError("Folgeseite außerhalb der FHIR-Abfrage.")
        if (inventory and total is None) or (total is not None and total != count):
            raise FhirError("FHIR-Bestand ist unvollständig.")

    def patients(self):
        patients = {}
        for page in self.pages("Patient", inventory=True):
            for entry in page.get("entry", []):
                patient = entry["resource"]
                pid = patient.get("id")
                if patient.get("resourceType") != "Patient" or not valid_id(pid) or pid in patients:
                    raise FhirError("Uneindeutiger Eintrag im Patientenbestand.")
                names = [name for name in patient.get("name", []) if isinstance(name, dict)]
                name = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
                parts = [*name.get("given", []), name.get("family")]
                label = name.get("text") or " ".join(p for p in parts if isinstance(p, str))
                patients[pid] = "".join(ch for ch in str(label) if ch.isprintable())
        return patients

    def download_pdf(self, attachment, path):
        url = attachment["url"]
        if url:
            if url.startswith("/cdn/"):
                host = self.origin.hostname
                url = f"https://{'[' + host + ']' if ':' in host else host}:16570" + url
            elif url.startswith(("/aps/fhir/api/r4/", "Binary/", "DocumentReference/")):
                url = urljoin(self.base + "/", url)
            stream = self.open(url, fhir=not safe_url(url).path.startswith("/cdn/"))
        else:
            stream = io.BytesIO(base64.b64decode(attachment["inlineData"], validate=True))
        digest, size = hashlib.sha256(), 0
        with stream, path.open("xb") as out:
            headers = getattr(stream, "headers", {})
            length = headers.get("Content-Length")
            if (getattr(stream, "status", 200) != 200 or
                    headers.get("Content-Encoding", "identity") != "identity" or
                    (length is not None and not 0 <= int(length) <= MAX_FILE)):
                raise FhirError("Ungültige Dateiübertragung.")
            while True:
                try:
                    chunk = stream.read(65536)
                except (OSError, http.client.HTTPException):
                    raise FhirError("PDF-Übertragung unterbrochen.") from None
                if not chunk:
                    break
                if not size and not chunk.startswith(b"%PDF-"):
                    raise FhirError("Datei enthält kein PDF.")
                size += len(chunk)
                if size > MAX_FILE:
                    raise FhirError("PDF überschreitet das Größenlimit.")
                digest.update(chunk)
                out.write(chunk)
        if not size or (length is not None and int(length) != size) or attachment["declaredSize"] not in (None, size):
            raise FhirError("PDF ist unvollständig.")
        return digest.hexdigest()
