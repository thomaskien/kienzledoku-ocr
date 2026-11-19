"""Serial, create-only OCR of every PDF in the visible FHIR patient inventory."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
import http.client
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin

from . import __version__
from .commander import CommanderError
from .errors import OcrError
from .fhir_client import FhirError, PROFILE, SessionError, check_outcome
from .fhir_model import FhirDataError, collect_entries, parse_document, plain_text, require_patient, valid_id
from .formatter import BLOCK_END, compose_text, contains_ocr_marker, first_text_lines, latest_ocr_marker, make_footer
from .progress import timed_step
from .pending_text import PendingText, PendingTextError

SOURCE = "KienzleOCR-Quelle "
MARK = re.compile(re.escape(SOURCE) + r"([a-f0-9]{64})\b")
DATA_ERRORS = (FhirError, ValueError, KeyError, TypeError, AttributeError, OcrError, http.client.HTTPException)


def reference(value, kind):
    prefix = kind + "/"
    if not isinstance(value, str) or not value.startswith(prefix) or not valid_id(value[len(prefix):]):
        raise FhirError(f"Ungültige Referenz für {kind}.")
    return value


def context_parts(data, identifier):
    if data.get("resourceType") != "Encounter" or data.get("id") != identifier:
        raise FhirError("T2med lieferte einen anderen Behandlungskontext.")
    people = {reference(p["individual"]["reference"], "Practitioner") for p in data["participant"]}
    if len(people) != 1:
        raise FhirError("Arztrolle im Kontext ist nicht eindeutig.")
    return (reference(data["subject"]["reference"], "Patient"), people.pop(),
            reference(data["serviceProvider"]["reference"], "Organization"))


class WriteContexts:
    def __init__(self, client, start):
        self.client, self.start = client, start
        self.use_start = True
        data, _, _ = client.json(client.base + "/Encounter/" + start)
        self.origin = context_parts(data, start)

    def for_patient(self, patient, current=None):
        subject = "Patient/" + patient
        _, doctor, org = self.origin
        identifier = current or (self.start if self.use_start and self.origin[0] == subject else None)
        if identifier:
            try:
                data, _, _ = self.client.json(self.client.base + "/Encounter/" + identifier)
            except FhirError as exc:
                if exc.status not in (404, 410):
                    raise
            else:
                if context_parts(data, identifier) != (subject, doctor, org):
                    raise SessionError("Patient, Arztrolle oder Behandlungsort im Schreibkontext weicht ab.")
                return identifier

        payload = {"resourceType": "Encounter", "meta": {"profile": [PROFILE + "Encounter|1.0.0"]},
                   "subject": {"reference": subject}, "serviceProvider": {"reference": org},
                   "participant": [{"individual": {"reference": doctor}}]}
        data, status, location = self.client.json(self.client.base + "/Encounter", body=payload)
        check_outcome(data)
        match = re.fullmatch(re.escape(self.client.base) + r"/Encounter/([A-Za-z0-9.-]{1,64})(?:/_history/[A-Za-z0-9.-]+)?",
                             urljoin(self.client.base + "/", location))
        if status != 201 or not match or not valid_id(match[1]):
            raise FhirError("Patientenkontext nicht bestätigt (Status/Location).")
        identifier = match[1]
        data, _, _ = self.client.json(self.client.base + "/Encounter/" + identifier)
        if context_parts(data, identifier) != (subject, doctor, org):
            raise SessionError("Patient, Arztrolle oder Behandlungsort im Schreibkontext weicht ab.")
        return identifier


def valid_date(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    return None


def original_date(attachment):
    # Preserve precision and offset. Never manufacture a clinical timestamp.
    for source in ("Composition.date", "attachment.creation", "DocumentReference.date"):
        dates = {v for k, v in attachment["dateCandidates"] if k == source}
        if dates:
            parsed = {valid_date(v) for v in dates}
            if len(parsed) != 1 or None in parsed:
                raise FhirError("Originaldatum fehlt oder ist mehrdeutig.")
            return sorted(dates)[0]
    raise FhirError("Originaldatum fehlt.")


def normalized(text):
    return " ".join(text.split())


@dataclass
class Chart:
    documents: dict = field(default_factory=dict)
    existing: dict = field(default_factory=dict)

    @classmethod
    def read(cls, client, patient):
        chart = cls()
        for page in client.pages(f"Patient/{patient}/$eakte"):
            for item in collect_entries(page, patient, client.base):
                old = chart.documents.setdefault(item["reference"], item)
                if old is not item:
                    old["entryText"] += "\n" + item["entryText"]
                    old["entryDates"].extend(item["entryDates"])
            for entry in page.get("entry", []):
                resource = entry["resource"]
                if resource.get("status") == "entered-in-error":
                    continue
                kind = resource.get("resourceType")
                if kind not in ("Composition", "Observation"):
                    continue
                require_patient(resource, patient, client.base)
                stamp = resource.get("date") if kind == "Composition" else resource.get("effectiveDateTime")
                texts = [resource.get("valueString", "")]
                sections = [resource]
                while sections:
                    section = sections.pop()
                    texts.append(plain_text(section.get("text", {}).get("div", "")))
                    sections.extend(section.get("section", []))
                for text in texts:
                    for key in MARK.findall(text):
                        chart.existing.setdefault(key, []).append((stamp, text))
        return chart


def create_text(client, context, text, stamp):
    obs = {"resourceType": "Observation", "meta": {"profile": [PROFILE + "ObservationFreitext|1.0.0"]},
           "identifier": [{"system": "https://fhir.t2med.de/identifier/kontext", "value": context}],
           "status": "final", "effectiveDateTime": stamp, "valueString": text,
           "extension": [{"url": PROFILE + "FreitextKuerzel", "valueString": "OCR"}]}
    data, status, _ = client.json(client.base + "/Observation", body=obs)
    if status != 201:
        raise FhirError("FHIR-Anlage nicht eindeutig bestätigt.")
    check_outcome(data)


class Backfill:
    def __init__(self, client, journal, ocr, report, *, apply=False, profile=__version__):
        self.client, self.journal, self.ocr, self.report = client, journal, ocr, report
        self.apply = apply
        self.server = sha256(client.base.encode()).hexdigest()
        self.pending = PendingText(journal.path, self.server, profile) if apply else None
        self.states = {r["fhirKey"]: r for r in journal.records()
                       if "fhirKey" in r and r.get("server") == self.server and
                       r.get("status") in {"create_prepared", "created_verified", "create_rejected"}}
        self.counts = Counter()
        self.current = {}

    def result(self, status, detail=""):
        self.counts[status] += 1
        self.journal.write({"fhirResult": status, **self.current})
        self.report(status + (" – " + detail if detail else ""))

    def process(self, start, *, patient_filter=None, limit=None, login=None):
        self.contexts = WriteContexts(self.client, start) if self.apply else None
        if login:
            self.client = login()
            if self.contexts:
                self.contexts.client = self.client
                self.contexts.use_start = False
        with timed_step("FHIR-Patienteninventar", self.report):
            patients = self.client.patients()
        self.report(f"FHIR-Inventar vollständig: {len(patients)} Patienten")
        if patient_filter:
            if patient_filter not in patients:
                raise FhirError("Testpatient fehlt im sichtbaren Bestand.")
            patients = {patient_filter: patients[patient_filter]}
        examined = 0
        for patient, name in patients.items():
            self.current = {"patient": patient}
            self.context = None
            self.report(f"Patient: {patient} {name}".rstrip())
            try:
                with timed_step("FHIR-eAkte lesen", self.report):
                    chart = Chart.read(self.client, patient)
            except SessionError:
                raise
            except DATA_ERRORS as exc:
                self.result("patient_failed", self.message(exc))
                continue
            for ref, entry in chart.documents.items():
                self.current = {"patient": patient, "documentReference": ref}
                try:
                    with timed_step("FHIR-Dokumentverweis lesen", self.report):
                        doc, _, _ = self.client.json(self.client.base + "/" + ref)
                    attachments = parse_document(doc, entry, patient, self.client.base)
                except SessionError:
                    raise
                except DATA_ERRORS as exc:
                    self.result("document_failed", self.message(exc))
                    continue
                for attachment in attachments:
                    self.current["attachment"] = attachment["index"]
                    mime = attachment["declaredMime"].split(";")[0].lower()
                    if mime not in {"application/pdf", "application/x-pdf"} and not (
                        mime in {"", "application/octet-stream"} and attachment["originalTitle"].lower().endswith(".pdf")
                    ):
                        self.result("unsupported_type")
                        continue
                    examined += 1
                    self.report(f"Identifiziere Dokument: {ref}, Anhang {attachment['index'] + 1}")
                    for label, value in (("Dokumententitel", entry["entryTitle"]), ("Dokumentenname", attachment["originalTitle"])):
                        self.report(label + ":")
                        for line in first_text_lines(value):
                            self.report("  " + "".join(c for c in line if c.isprintable()))
                    try:
                        with timed_step("Gesamtzeit Dokument", self.report):
                            self.attachment(patient, ref, attachment, chart)
                    except SessionError:
                        raise
                    except DATA_ERRORS as exc:
                        self.result("document_failed", self.message(exc))
                    self.report("Dokument fertig")
                    if limit and examined >= limit:
                        return

    @staticmethod
    def message(exc):
        # Arbitrary payloads, malformed URLs and OAuth credentials never enter progress.
        return str(exc) if isinstance(exc, (FhirError, FhirDataError, CommanderError, PendingTextError)) else type(exc).__name__

    def attachment(self, patient, ref, attachment, chart):
        if contains_ocr_marker(attachment["entryText"]):
            self.already_ocr("OCR-Markierung am Original", [attachment["entryText"]])
            return
        stamp = original_date(attachment)
        self.report("Originaldatum " + stamp)
        with tempfile.TemporaryDirectory(prefix="kienzleocr-") as work:
            path = Path(work) / "input.pdf"
            self.report("Dokument wird geladen")
            with timed_step("Datei laden", self.report):
                digest = self.client.download_pdf(attachment, path)
            identity = [patient, ref, attachment["index"], digest]
            key = sha256(json.dumps(identity).encode()).hexdigest()
            previous = self.states.get(key)
            if previous and previous["status"] == "create_prepared":
                if self.verified(chart, previous):
                    self.record(previous, "created_verified")
                    if self.pending:
                        self.pending.discard(key)
                    self.result("already_ocr", "Frühere Anlage jetzt in der Akte bestätigt")
                else:
                    self.result("unconfirmed", "Früherer Schreibausgang offen. Journal und eAkte prüfen.")
                return
            if key in chart.existing:
                if self.pending:
                    self.pending.discard(key)
                self.already_ocr("zugehöriger OCR-Eintrag", [text for _, text in chart.existing[key]])
                return
            if previous and previous["status"] == "created_verified":
                self.result("unconfirmed", "Früher bestätigter Eintrag aktuell nicht in der eAkte sichtbar.")
                return
            self.report("OCR-Status: noch nicht erfolgt")
            text = self.pending.read(key, stamp) if self.pending else None
            if text is None:
                self.report("OCR läuft")
                with timed_step("OCR gesamt", self.report):
                    text = self.ocr.extract_text(path, "application/pdf")
                if self.pending and text.strip():
                    self.pending.store(key, stamp, text)
            else:
                self.report("Vorbereiteten OCR-Text für dieselbe PDF-Prüfsumme wiederverwendet")
        if not text.strip():
            raise FhirError("OCR ergab keinen Text.")
        self.report("OCR erfolgreich")
        entry_text = compose_text(SOURCE + key, text, make_footer(__version__))
        if not self.apply:
            self.result("dry_run", f"OCR-Eintrag mit {len(text)} Zeichen würde angelegt")
            return
        # Recheck after potentially long OCR. Original may have gained a legacy block.
        fresh = Chart.read(self.client, patient)
        if key in fresh.existing:
            self.pending.discard(key)
            self.result("already_ocr")
            return
        if ref not in fresh.documents:
            raise FhirError("Dokument inzwischen aus der eAkte entfernt.")
        doc, _, _ = self.client.json(self.client.base + "/" + ref)
        current = next((a for a in parse_document(doc, fresh.documents[ref], patient, self.client.base)
                        if a["index"] == attachment["index"]), None)
        if current is None or original_date(current) != stamp:
            raise FhirError("Dokument oder Originaldatum inzwischen geändert.")
        if contains_ocr_marker(current["entryText"]):
            self.pending.discard(key)
            self.result("already_ocr")
            return
        with tempfile.TemporaryDirectory(prefix="kienzleocr-check-") as work:
            if self.client.download_pdf(current, Path(work) / "input.pdf") != digest:
                raise FhirError("Quelldatei inzwischen geändert.")
        record = {"fhirKey": key, "server": self.server, "patient": patient, "documentReference": ref, "attachment": attachment["index"],
                  "fileSha256": digest, "textSha256": sha256(normalized(entry_text).encode()).hexdigest(),
                  "originalDate": stamp}
        # Revalidate immediately before writing, even for consecutive documents of one patient.
        self.context = self.contexts.for_patient(patient, self.context)
        self.record(record, "create_prepared")
        try:
            with timed_step("FHIR-Freitext anlegen", self.report):
                create_text(self.client, self.context, entry_text, stamp)
        except DATA_ERRORS as exc:
            if getattr(exc, "status", None) in {400, 401, 403, 404, 405, 422}:
                self.record(record, "create_rejected")
                raise
            self.report("Schreibantwort uneindeutig. Eintrag wird in der Patientenakte nachgelesen.")
        try:
            with timed_step("FHIR-Freitext nachlesen", self.report):
                readback = Chart.read(self.client, patient)
        except SessionError:
            raise
        except DATA_ERRORS:
            self.result("unconfirmed", "Nachlesen fehlgeschlagen. Journal für den Wiederanlauf erhalten.")
            return
        if not self.verified(readback, record):
            self.result("unconfirmed", "Anlage gemeldet, Text und Datum in der eAkte noch nicht bestätigt.")
            return
        self.record(record, "created_verified")
        self.pending.discard(key)
        self.result("created_verified", "OCR-Eintrag in der Patientenakte nachgelesen")

    def already_ocr(self, description, texts):
        for text in texts:
            marker = latest_ocr_marker(text)
            if marker:
                version, when = marker
                description += f", Version {version} am {when}"
                break
        self.result("already_ocr", description)

    def record(self, record, status):
        row = {**record, "status": status}
        row.pop("timestamp", None)
        self.journal.write(row)
        self.states[row["fhirKey"]] = row

    @staticmethod
    def verified(chart, record):
        key = record["fhirKey"]
        for stamp, text in chart.existing.get(key, []):
            if valid_date(stamp) == valid_date(record["originalDate"]):
                body = normalized(text)
                start = body.find(SOURCE + key)
                stop = body.rfind(BLOCK_END)
                if start >= 0 and stop > start:
                    block = body[start:stop + len(BLOCK_END)]
                    if sha256(block.encode()).hexdigest() == record["textSha256"]:
                        return True
        return False
