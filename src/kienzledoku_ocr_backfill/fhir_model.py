"""Pure T2med FHIR response mapping; no I/O and no raw HTML in viewer text."""
from html.parser import HTMLParser
import re


class FhirDataError(ValueError):
    pass


ID = re.compile(r"[A-Za-z0-9.-]{1,64}\Z")


def valid_id(value):
    return isinstance(value, str) and bool(ID.fullmatch(value)) and value not in {".", ".."}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif not self.hidden and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    if not isinstance(value, str):
        return ""
    parser = _Text()
    parser.feed(value)
    parser.close()
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(parser.parts)).strip()


def _join(values):
    return "\n\n".join(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))


def require_patient(resource, patient_id, base_url):
    if resource.get("subject", {}).get("reference") not in {"Patient/" + patient_id, base_url + "/Patient/" + patient_id}:
        raise FhirDataError("FHIR-Akteneintrag gehört nicht eindeutig zum freigegebenen Patienten.")


def document_reference(value, base_url):
    if not isinstance(value, str):
        return None
    path = value[len(base_url)+1:] if value.startswith(base_url + "/") else value
    if path.startswith("DocumentReference/"):
        identifier = path[len("DocumentReference/"):]
        if not valid_id(identifier):
            raise FhirDataError("Ungültiger FHIR-Dokumentverweis.")
        return path
    if "DocumentReference/" in value:
        raise FhirDataError("FHIR-Dokumentverweis liegt außerhalb des freigegebenen Dienstes.")
    return None


def collect_entries(bundle, patient_id, base_url):
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle" or not isinstance(bundle.get("entry", []), list):
        raise FhirDataError("FHIR-eAkte liefert keine auswertbare Eintragsliste.")
    result = {}
    for entry in bundle.get("entry", []):
        composition = entry.get("resource", {})
        if composition.get("resourceType") != "Composition" or composition.get("status") == "entered-in-error":
            continue
        require_patient(composition, patient_id, base_url)
        root_text = plain_text(composition.get("text", {}).get("div", ""))
        stack = [(s, [root_text]) for s in reversed(composition.get("section", []))]
        while stack:
            section, parent_texts = stack.pop()
            texts = parent_texts + [plain_text(section.get("text", {}).get("div", ""))]
            for item in section.get("entry", []):
                ref = document_reference(item.get("reference"), base_url)
                if not ref:
                    continue
                record = result.setdefault(ref, {"reference": ref, "entryTitle": str(composition.get("title") or ""), "entryText": "", "entryDates": [], "_texts": []})
                if not record["entryTitle"]:
                    record["entryTitle"] = str(composition.get("title") or "")
                for text in texts:
                    if text and text not in record["_texts"]:
                        record["_texts"].append(text)
                record["entryText"] = _join(record["_texts"])
                date = composition.get("date")
                if isinstance(date, str) and date and date not in record["entryDates"]:
                    record["entryDates"].append(date)
            stack.extend((child, texts) for child in reversed(section.get("section", [])))
    for record in result.values():
        record.pop("_texts", None)
    return list(result.values())


def parse_document(document, entry, patient_id, base_url):
    if (not isinstance(document, dict) or document.get("resourceType") != "DocumentReference"
            or not valid_id(document.get("id")) or "DocumentReference/" + document["id"] != entry["reference"]):
        raise FhirDataError("FHIR lieferte nicht das angeforderte Dokument.")
    require_patient(document, patient_id, base_url)
    if document.get("status") == "entered-in-error":
        return []
    description = str(document.get("description") or "")
    result = []
    for index, content in enumerate(document.get("content", [])):
        attachment = content.get("attachment", {})
        url, data = attachment.get("url") or "", attachment.get("data") or ""
        if not url and not data:
            continue
        if not isinstance(url, str) or not isinstance(data, str):
            raise FhirDataError("FHIR-Dateianhang hat ein ungültiges Format.")
        size = attachment.get("size")
        dates = [("Composition.date", v) for v in entry.get("entryDates", [])]
        dates += [(key, value) for key, value in (("attachment.creation", attachment.get("creation")), ("DocumentReference.date", document.get("date"))) if isinstance(value, str) and value]
        result.append({"index": index, "url": url, "inlineData": data, "declaredMime": str(attachment.get("contentType") or ""),
                       "originalTitle": str(attachment.get("title") or entry.get("entryTitle") or description or "Dokument"),
                       "declaredSize": size if type(size) is int and size >= 0 else None,
                       "entryText": _join([entry.get("entryText", ""), description]), "dateCandidates": dates})
    return result
