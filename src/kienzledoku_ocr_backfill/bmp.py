"""Parser and human-readable formatter for the KBV BMP Data-Matrix payload."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .bfarm_pzn import normalize_pzn


SECTION_CODES = {
    "411": "Bedarfsmedikation",
    "412": "Dauermedikation",
    "413": "Intramuskuläre Anwendung",
    "414": "Besondere Anwendung",
    "415": "Intravenöse Anwendung",
    "416": "Anwendung unter die Haut",
    "417": "Fertigspritze",
    "418": "Selbstmedikation",
    "419": "Allergiehinweise",
    "421": "Wichtige Hinweise",
    "422": "Wichtige Angaben",
    "423": "Zu besonderen Zeiten anzuwendende Medikamente",
    "424": "Zeitlich befristet anzuwendende Medikamente",
    "425": "Wöchentliche Anwendung",
}

DOSAGE_UNITS = {
    "#": "Messlöffel",
    "0": "Messbecher",
    "1": "Stück",
    "2": "Pkg.",
    "3": "Flasche",
    "4": "Beutel",
    "5": "Hub",
    "6": "Tropfen",
    "7": "Teelöffel",
    "8": "Esslöffel",
    "9": "E",
    "a": "Tasse",
    "b": "Applikatorfüllung",
    "c": "Augenbadewanne",
    "d": "Dosierbriefchen",
    "e": "Dosierpipette",
    "f": "Dosierspritze",
    "g": "Einzeldosis",
    "h": "Glas",
    "i": "Likörglas",
    "j": "Messkappe",
    "k": "Messschale",
    "l": "Mio. E",
    "m": "Mio. IE",
    "n": "Pipettenteilstrich",
    "o": "Sprühstoß",
    "p": "IE",
    "q": "cm",
    "r": "l",
    "s": "ml",
    "t": "g",
    "u": "kg",
    "v": "mg",
}

WEEKDAYS = {
    "1": "Montag",
    "2": "Dienstag",
    "3": "Mittwoch",
    "4": "Donnerstag",
    "5": "Freitag",
    "6": "Samstag",
    "7": "Sonntag",
}

class BmpParseError(ValueError):
    pass


@dataclass(frozen=True)
class BmpSubstance:
    name: Optional[str]
    strength: Optional[str]


@dataclass(frozen=True)
class BmpMedication:
    pzn: Optional[str]
    name: Optional[str]
    form: Optional[str]
    substances: tuple[BmpSubstance, ...]
    morning: Optional[str]
    noon: Optional[str]
    evening: Optional[str]
    night: Optional[str]
    dosage_text: Optional[str]
    dosage_unit: Optional[str]
    weekday: Optional[str]
    instructions: Optional[str]
    reason: Optional[str]
    extra: Optional[str]


@dataclass(frozen=True)
class BmpFreeText:
    text: str


@dataclass(frozen=True)
class BmpRecipe:
    text: str
    extra: Optional[str]


@dataclass(frozen=True)
class BmpSection:
    title: Optional[str]
    entries: tuple[Any, ...]


@dataclass(frozen=True)
class BmpPlan:
    version: str
    plan_id: Optional[str]
    language: Optional[str]
    page: Optional[int]
    total_pages: Optional[int]
    patient: dict[str, Optional[str]]
    author: dict[str, Optional[str]]
    parameters: dict[str, Optional[str]]
    sections: tuple[BmpSection, ...]


@dataclass(frozen=True)
class FormattedBmp:
    text: str
    pzns: tuple[str, ...]
    unresolved_pzns: tuple[str, ...]


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.replace("~", "\n").strip()
    return cleaned or None


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _medication(node: ET.Element) -> BmpMedication:
    substances = tuple(
        BmpSubstance(_clean(child.get("w")), _clean(child.get("s")))
        for child in node.findall("W")
    )
    raw_pzn = _clean(node.get("p"))
    try:
        pzn = normalize_pzn(raw_pzn) if raw_pzn else None
    except ValueError as exc:
        raise BmpParseError(f"Ungültige BMP-PZN: {raw_pzn!r}") from exc
    unit = _clean(node.get("dud"))
    if unit is None and node.get("du") is not None:
        unit = DOSAGE_UNITS.get(node.get("du"), f"Einheit-Code {node.get('du')}")
    return BmpMedication(
        pzn=pzn,
        name=_clean(node.get("a")),
        form=_clean(node.get("fd")) or _clean(node.get("f")),
        substances=substances,
        morning=_clean(node.get("m")),
        noon=_clean(node.get("d")),
        evening=_clean(node.get("v")),
        night=_clean(node.get("h")),
        dosage_text=_clean(node.get("t")),
        dosage_unit=unit,
        weekday=WEEKDAYS.get(node.get("wo"), _clean(node.get("wo"))),
        instructions=_clean(node.get("i")),
        reason=_clean(node.get("r")),
        extra=_clean(node.get("x")),
    )


def parse_bmp(data: bytes) -> Optional[BmpPlan]:
    """Parse a BMP payload; return None when the payload is not BMP XML."""
    if len(data) > 65536:
        raise BmpParseError("BMP-Payload überschreitet 64 KiB")
    text = data.decode("iso-8859-1").lstrip("\ufeff\x00 \t\r\n")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise BmpParseError("DTD/Entities sind im BMP nicht zulässig")
    if not text.startswith("<MP"):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise BmpParseError(f"BMP-XML ist nicht wohlgeformt: {exc}") from exc
    if root.tag != "MP":
        return None
    version = root.get("v") or ""
    if re.fullmatch(r"[0-9]{3}", version) is None:
        raise BmpParseError("BMP-Versionsnummer fehlt oder ist ungültig")

    patient_node = root.find("P")
    author_node = root.find("A")
    parameter_node = root.find("O")
    patient = {
        "given": _clean(patient_node.get("g")) if patient_node is not None else None,
        "family": _clean(patient_node.get("f")) if patient_node is not None else None,
        "title": _clean(patient_node.get("t")) if patient_node is not None else None,
        "prefix": _clean(patient_node.get("v")) if patient_node is not None else None,
        "suffix": _clean(patient_node.get("z")) if patient_node is not None else None,
        "insuranceId": _clean(patient_node.get("egk")) if patient_node is not None else None,
        "birthDate": _clean(patient_node.get("b")) if patient_node is not None else None,
        "gender": _clean(patient_node.get("s")) if patient_node is not None else None,
    }
    author = {
        key: _clean(author_node.get(attribute)) if author_node is not None else None
        for key, attribute in {
            "name": "n",
            "street": "s",
            "postalCode": "z",
            "city": "c",
            "phone": "p",
            "email": "e",
            "printedAt": "t",
            "lanr": "lanr",
            "idf": "idf",
            "kik": "kik",
        }.items()
    }
    parameters = {
        key: _clean(parameter_node.get(attribute)) if parameter_node is not None else None
        for key, attribute in {
            "weight": "w",
            "height": "h",
            "creatinine": "c",
            "allergies": "ai",
            "nursing": "b",
            "pregnant": "p",
            "freeText": "x",
        }.items()
    }

    sections: list[BmpSection] = []
    for section_node in root.findall("S"):
        title = _clean(section_node.get("t"))
        if title is None and section_node.get("c"):
            title = SECTION_CODES.get(
                section_node.get("c"), f"Zwischenüberschrift {section_node.get('c')}"
            )
        entries: list[Any] = []
        for node in section_node:
            if node.tag == "M":
                entries.append(_medication(node))
            elif node.tag == "X" and _clean(node.get("t")):
                entries.append(BmpFreeText(_clean(node.get("t")) or ""))
            elif node.tag == "R" and _clean(node.get("t")):
                entries.append(
                    BmpRecipe(
                        _clean(node.get("t")) or "",
                        _clean(node.get("x")),
                    )
                )
        sections.append(BmpSection(title, tuple(entries)))

    return BmpPlan(
        version=version,
        plan_id=_clean(root.get("U")),
        language=_clean(root.get("l")),
        page=_int(root.get("a")),
        total_pages=_int(root.get("z")),
        patient=patient,
        author=author,
        parameters=parameters,
        sections=tuple(sections),
    )


def _date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    for source, target in (("%Y%m%d", "%d.%m.%Y"), ("%Y%m", "%m.%Y"), ("%Y", "%Y")):
        try:
            return datetime.strptime(value, source).strftime(target)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def _version(value: str) -> str:
    return f"{int(value[:2])}.{value[2]}" if len(value) == 3 else value


def _person_name(patient: dict[str, Optional[str]]) -> str:
    return " ".join(
        part
        for part in (
            patient.get("title"),
            patient.get("given"),
            patient.get("prefix"),
            patient.get("family"),
            patient.get("suffix"),
        )
        if part
    ) or "(nicht angegeben)"


def _resolved_medication(
    medication: BmpMedication, resolver: Optional[Any]
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if medication.pzn is None or resolver is None:
        return None, medication.pzn
    return resolver.lookup(medication.pzn), None


def _substances(
    medication: BmpMedication, resolved: Optional[dict[str, Any]]
) -> list[BmpSubstance]:
    if medication.substances:
        return list(medication.substances)
    if not resolved:
        return []
    return [
        BmpSubstance(_clean(entry.get("name")), _clean(entry.get("strength")))
        for entry in resolved.get("substances", [])
    ]


def _one_line(value: Optional[str], fallback: str = "-") -> str:
    compact = " ".join((value or "").split())
    return compact or fallback


def format_bmp(plan: BmpPlan, resolver: Optional[Any] = None) -> FormattedBmp:
    birth_date = _date(plan.patient.get("birthDate")) or "(nicht angegeben)"
    lines = [
        "----- BEGINN BUNDESMEDIKATIONSPLAN -----",
        (
            f"BUNDESMEDIKATIONSPLAN für {_person_name(plan.patient)}, "
            f"Geburtsdatum: {birth_date}"
        ),
        f"Ausstellungsdatum: {_date(plan.author.get('printedAt')) or '(nicht angegeben)'}",
        f"Ausgestellt durch: {plan.author.get('name') or '(nicht angegeben)'}",
    ]
    plan_details = [f"BMP-Version {_version(plan.version)}"]
    if plan.page or plan.total_pages:
        plan_details.append(f"Seite {plan.page or '?'} von {plan.total_pages or '?'}")
    if plan.plan_id:
        plan_details.append(f"Plan-ID {plan.plan_id}")
    lines.append(" | ".join(plan_details))

    parameter_lines: list[str] = []
    for key, label, suffix in (
        ("weight", "Gewicht", " kg"),
        ("height", "Größe", " cm"),
        ("creatinine", "Kreatinin", " mg/dl"),
        ("allergies", "Allergien/Unverträglichkeiten", ""),
    ):
        if plan.parameters.get(key):
            parameter_lines.append(f"{label}: {plan.parameters[key]}{suffix}")
    if plan.parameters.get("pregnant") == "1":
        parameter_lines.append("schwanger")
    if plan.parameters.get("nursing") == "1":
        parameter_lines.append("stillend")
    if plan.parameters.get("freeText"):
        parameter_lines.extend((plan.parameters["freeText"] or "").splitlines())
    if parameter_lines:
        lines.extend(["", "Patientenparameter:"])
        lines.extend(f"- {line}" for line in parameter_lines)

    pzns: list[str] = []
    unresolved: list[str] = []
    medication_number = 0
    for section in plan.sections:
        table_rows: list[tuple[str, str, str, str, Optional[str]]] = []
        section_comments: list[str] = []
        for entry in section.entries:
            if isinstance(entry, BmpFreeText):
                section_comments.extend(entry.text.splitlines())
                continue
            if isinstance(entry, BmpRecipe):
                section_comments.append(f"Rezeptur: {_one_line(entry.text)}")
                if entry.extra:
                    section_comments.append(f"Rezeptur-Zusatz: {_one_line(entry.extra)}")
                continue
            if not isinstance(entry, BmpMedication):
                continue
            medication_number += 1
            resolved, failed_pzn = _resolved_medication(entry, resolver)
            if entry.pzn:
                pzns.append(entry.pzn)
            if failed_pzn or (entry.pzn and resolved is None):
                unresolved.append(entry.pzn or failed_pzn or "")
            name = entry.name or (resolved or {}).get("name")
            medication_name = name or "Arzneimittel nicht aufgelöst"
            substance_values = []
            strength_values = []
            for substance in _substances(entry, resolved):
                value = substance.name or "(unbekannt)"
                if substance.strength:
                    strength_values.append(substance.strength)
                substance_values.append(value)
            if substance_values:
                medication_name += f" ({'; '.join(substance_values)})"
            form = entry.form or (resolved or {}).get("form_long") or (resolved or {}).get("form_short")
            dose_parts = list(dict.fromkeys(strength_values))
            if form:
                dose_parts.append(form)
            dose = ", ".join(dose_parts) or "-"
            structured_dosage = any(
                (entry.morning, entry.noon, entry.evening, entry.night)
            )
            morning = entry.morning or ("0" if structured_dosage else "-")
            noon = entry.noon or ("0" if structured_dosage else "-")
            evening = entry.evening or ("0" if structured_dosage else "-")
            night = entry.night or ("0" if structured_dosage else "-")
            comments: list[str] = []
            if entry.weekday:
                comments.append(f"wöchentlich am {entry.weekday}")
            if entry.instructions:
                comments.append(_one_line(entry.instructions))
            if entry.reason:
                comments.append(f"Grund: {_one_line(entry.reason)}")
            if entry.extra:
                comments.append(_one_line(entry.extra))
            if entry.pzn:
                comments.append(f"PZN {entry.pzn}")
            if structured_dosage:
                intake = "-".join((morning, noon, evening, night))
            elif entry.dosage_text:
                intake = _one_line(entry.dosage_text)
            else:
                intake = "-"
            if entry.dosage_unit and intake != "-":
                intake += f" {entry.dosage_unit}"
            table_rows.append(
                (
                    str(medication_number),
                    medication_name,
                    dose,
                    intake,
                    "; ".join(comments) or None,
                )
            )

        lines.extend(["", f"Überschrift: {section.title or 'Medikation'}"])
        if table_rows:
            lines.append("## Nr\t| Medikament\t| Dosis\t| Einnahme")
            for number, medication, dose, intake, comment in table_rows:
                lines.append(f"{number}\t| {medication}\t| {dose}\t| {intake}")
                if comment:
                    lines.append(f"\tKommentar: {comment}")
        for comment in section_comments:
            lines.append(f"Kommentar: {comment}")

    lines.extend(["", "----- ENDE BUNDESMEDIKATIONSPLAN -----"])

    return FormattedBmp(
        text="\n".join(lines).strip(),
        pzns=tuple(dict.fromkeys(pzns)),
        unresolved_pzns=tuple(dict.fromkeys(value for value in unresolved if value)),
    )
