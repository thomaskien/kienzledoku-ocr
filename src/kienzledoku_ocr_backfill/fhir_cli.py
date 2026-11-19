"""Linux entry point for T2med's kienzleocr:// system handler."""

import argparse
import http.client
import json
import os
from pathlib import Path
import ssl
import sys
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .fhir_backfill import Backfill
from .fhir_client import Client, FhirError
from .fhir_device import device_client
from .fhir_model import valid_id
from .journal import Journal
from .ocr import OcrmypdfBackend
from .progress import TimestampedReporter


def parse_link(raw, base):
    if len(raw) > 32768 or any(ord(c) < 32 for c in raw):
        raise FhirError("Ungültiger T2med-Aufruf.")
    link = urlsplit(raw)
    if (link.scheme != "kienzleocr" or link.netloc not in ("", "start") or link.path not in ("", "/")
            or link.fragment):
        raise FhirError("Erwartet wird kienzleocr://start mit T2med-Startparametern.")
    try:
        params = parse_qs(link.query, strict_parsing=True, keep_blank_values=True, max_num_fields=3)
    except ValueError:
        raise FhirError("Ungültige Startparameter.") from None
    if (set(params) != {"kontextId", "oAuthToken", "fhirBasisUrl"} or
            any(len(v) != 1 or not v[0] or "${" in v[0] for v in params.values())):
        raise FhirError("Startparameter fehlen oder sind mehrdeutig.")
    context = params["kontextId"][0]
    if params["fhirBasisUrl"][0].rstrip("/") != base.rstrip("/") or not valid_id(context):
        raise FhirError("FHIR-Ziel oder Startkontext ungültig. Lokale Konfiguration prüfen.")
    return context, params["oAuthToken"][0]


def main(argv=None):
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    parser = argparse.ArgumentParser(prog="kienzledoku-ocr fhir", description="OCR über alle sichtbaren FHIR-Patienten")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, default=config_home / "kienzleocr/config.json")
    parser.add_argument("--journal", type=Path, default=state_home / "kienzleocr/fhir.jsonl")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="Neue OCR-Freitexte anlegen")
    modes.add_argument("--dry-run", action="store_true", help="OCR ausführen und Ergebnis zählen (Standard)")
    parser.add_argument("--patient", help="Test auf eine FHIR-Patienten-ID begrenzen")
    parser.add_argument("--limit", type=int, help="Maximale Anzahl PDF-Kandidaten für einen Test")
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--auto-orient-pages", action="store_true")
    parser.add_argument("--barcode-retry-dpi", type=int, default=300)
    parser.add_argument("uri", nargs="?", help="T2med-Link (alternativ eine Zeile auf stdin)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit muss positiv sein")
    report = TimestampedReporter()
    try:
        if args.config.stat().st_mode & 0o077:
            raise FhirError("FHIR-Konfiguration mit chmod 600 schützen.")
        cfg = json.loads(args.config.read_text(encoding="utf-8"))
        base = cfg["base_url"]
        ctx, token = parse_link(args.uri or sys.stdin.readline(32770).rstrip("\r\n"), base)
        tls = ssl.create_default_context(cafile=cfg.get("ca_file") or None)
        client = Client(base, token, cfg["api_key"], tls)
        ocr = OcrmypdfBackend(progress=report, auto_orient_pages=args.auto_orient_pages,
                              barcode_retry_dpi=args.barcode_retry_dpi, amdb_remote=cfg.get("amdb_remote"))
        report(f"KienzleDoku OCR {__version__} – " + ("APPLY" if args.apply else "DRY-RUN"))
        with Journal(args.journal) as journal:
            run = Backfill(client, journal, ocr, report, apply=args.apply)
            login = None
            if cfg.get("client_id"):
                login = lambda: device_client(base, cfg["api_key"], cfg["client_id"], tls, report)
            run.process(ctx, patient_filter=args.patient, limit=args.limit, login=login)
            report("Zusammenfassung " + json.dumps(run.counts, sort_keys=True))
            return 2 if {"patient_failed", "document_failed", "unconfirmed"} & run.counts.keys() else 0
    except KeyboardInterrupt:
        return 130
    except (FhirError, ValueError, OSError, TypeError, KeyError, AttributeError, RuntimeError, http.client.HTTPException) as exc:
        detail = str(exc) if isinstance(exc, FhirError) or type(exc) is RuntimeError else type(exc).__name__
        report(f"FHIR-Lauf abgebrochen ({detail}). Zugang, Konfiguration und Journal prüfen.")
        return 1
    finally:
        if args.desktop and sys.stdin.isatty():
            try:
                input("Zum Schließen Eingabetaste drücken.")
            except (EOFError, KeyboardInterrupt):
                pass
