"""Command-line entry point. Default mode is always dry-run."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import sys
import urllib.parse
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .aps_client import ApsClient
from .cdn_client import CdnClient
from .config import T2medConfig
from .db_reader import DatabaseReader
from .errors import BackfillError
from .handlers.document_reference import DocumentReferenceHandler
from .http_client import HttpClient, make_ssl_context
from .journal import Journal
from .ocr.command import CommandOcrBackend
from .ocr.ocrmypdf import OcrmypdfBackend
from .processor import BackfillProcessor
from .rollback import RollbackProcessor


def _server_name(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise argparse.ArgumentTypeError("Servername darf nicht leer sein")
    parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise argparse.ArgumentTypeError("Server muss ein Hostname oder eine HTTPS-URL sein")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("Server darf keinen Pfad, Query oder Fragment enthalten")
    return parsed.hostname


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("Wert muss mindestens 1 sein")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("Wert darf nicht negativ sein")
    return parsed


def _forced_page_rotation(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"([1-9][0-9]*):([+-](?:90|180|270))", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "Format muss SEITE:+WINKEL sein, zum Beispiel 1:+90"
        )
    return int(match.group(1)), match.group(2)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OCR-Text seriell und journalisiert an T2med-PDF-Dokumentverweise anhängen. "
            "Ohne --apply werden niemals APS-Änderungen geschrieben."
        )
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Keine APS-Updates (Standard)")
    mode.add_argument("--apply", action="store_true", help="APS-Updates wirklich ausführen")
    parser.add_argument("--limit", type=_positive_int, help="Höchstens N Inventareinträge verarbeiten")
    parser.add_argument("--resume", action="store_true", help="Erfolgreiche objectIds aus dem Journal überspringen")
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help=(
            "Vorhandenen KienzleDoku-OCR-Block sicher ersetzen und OCR erneut ausführen; "
            "nicht mit --resume kombinierbar"
        ),
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help=(
            "Verifizierte Updates aus dem Journal zurückrollen. Ohne --apply nur "
            "Konfliktprüfung; mit --apply echte Rücksetzung."
        ),
    )
    parser.add_argument("--patient", help="Nur diese T2med-Patientennummer verarbeiten")
    parser.add_argument("--object-id", help="Nur diese Dokument-objectId verarbeiten")
    parser.add_argument(
        "--journal",
        type=Path,
        default=Path("kienzledoku-ocr.jsonl"),
        help="JSONL-Journal; Standard ./kienzledoku-ocr.jsonl",
    )

    parser.add_argument("--server", type=_server_name, default="127.0.0.1")
    parser.add_argument("--aps-port", type=int, default=16567)
    parser.add_argument("--cdn-port", type=int, default=16570)
    parser.add_argument("--username", help="T2med-Benutzername; alternativ T2MED_OCR_USERNAME")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP-Timeout in Sekunden")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--insecure", action="store_true", help="TLS-Zertifikatsprüfung deaktivieren")
    tls.add_argument("--ca-cert", type=Path, help="Vertrauenswürdige PEM-CA für T2med")

    parser.add_argument(
        "--psql",
        type=Path,
        default=Path("/opt/t2med/server/postgres/bin/psql"),
        help="Pfad zum von T2med mitgelieferten psql",
    )
    parser.add_argument("--db-host", default="/tmp")
    parser.add_argument("--db-port", type=int, default=16569)
    parser.add_argument("--db-user", default="t2med")
    parser.add_argument("--db-name", default="t2med")

    parser.add_argument(
        "--ocr-command",
        help=(
            "Optionaler eigener OCR-Befehl mit {input}; überschreibt das standardmäßige "
            "KienzleFax-OCRmyPDF-Backend. Alternativ KIENZLEDOKU_OCR_COMMAND."
        ),
    )
    parser.add_argument("--ocr-timeout", type=float, default=1800.0)
    parser.add_argument("--ocrmypdf", default="ocrmypdf", help="OCRmyPDF-Programm oder absoluter Pfad")
    parser.add_argument("--pdftotext", default="pdftotext", help="pdftotext-Programm oder absoluter Pfad")
    parser.add_argument("--pdftoppm", default="pdftoppm", help="pdftoppm-Programm oder absoluter Pfad")
    parser.add_argument("--qpdf", default="qpdf", help="qpdf-Programm oder absoluter Pfad")
    parser.add_argument("--tesseract", default="tesseract", help="Tesseract-Programm oder absoluter Pfad")
    parser.add_argument("--ocr-language", default="deu+eng", help="Tesseract-Sprachen; Standard deu+eng")
    parser.add_argument("--ocr-jobs", type=_positive_int, default=2, help="Interne OCRmyPDF-Jobs; Standard 2")
    parser.add_argument(
        "--rotate-pages-threshold",
        type=_nonnegative_float,
        default=14.0,
        help=(
            "Nachgelagerte OCRmyPDF-Schwelle für automatische Seitendrehung; "
            "Standard 14"
        ),
    )
    parser.add_argument(
        "--force-rotate-page",
        action="append",
        type=_forced_page_rotation,
        default=[],
        metavar="SEITE:+WINKEL",
        help=(
            "Eine PDF-Seite in der temporären OCR-Kopie ausdrücklich drehen, "
            "z. B. 1:+90; für mehrere Seiten wiederholen"
        ),
    )
    parser.add_argument(
        "--no-auto-orient-pages",
        dest="auto_orient_pages",
        action="store_false",
        default=True,
        help="Automatische seitenweise Orientierungsprüfung deaktivieren",
    )
    parser.add_argument(
        "--orientation-confidence",
        type=_nonnegative_float,
        default=5.0,
        help="Mindestkonfidenz für direkte Tesseract-OSD-Drehung; Standard 5",
    )
    parser.add_argument(
        "--tesseract-timeout",
        type=float,
        default=300.0,
        help="Tesseract-Zeitlimit je Seite; Standard 300 Sekunden",
    )
    return parser.parse_args(argv)


def _credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = (args.username or os.environ.get("T2MED_OCR_USERNAME", "")).strip()
    if not username:
        try:
            username = input("T2med-Benutzername: ").strip()
        except (EOFError, OSError) as exc:
            raise BackfillError("T2med-Benutzername konnte nicht abgefragt werden") from exc
    if not username:
        raise BackfillError("T2med-Benutzername fehlt")

    if "T2MED_OCR_PASSWORD" in os.environ:
        password = os.environ.get("T2MED_OCR_PASSWORD", "")
    else:
        try:
            password = getpass.getpass("T2med-Passwort: ")
        except (EOFError, OSError) as exc:
            raise BackfillError("T2med-Passwort konnte nicht abgefragt werden") from exc
    return username, password


def _build_config(args: argparse.Namespace) -> T2medConfig:
    if args.timeout <= 0 or args.ocr_timeout <= 0 or args.tesseract_timeout <= 0:
        raise BackfillError("Timeouts müssen größer als 0 sein")
    for name in ("aps_port", "cdn_port", "db_port"):
        value = getattr(args, name)
        if not 1 <= value <= 65535:
            raise BackfillError(f"--{name.replace('_', '-')} ist kein gültiger Port")
    if args.ca_cert is not None and not args.ca_cert.is_file():
        raise BackfillError(f"CA-Datei nicht gefunden: {args.ca_cert}")
    return T2medConfig(
        server=args.server,
        aps_port=args.aps_port,
        cdn_port=args.cdn_port,
        timeout=args.timeout,
        insecure=args.insecure,
        ca_cert=args.ca_cert,
        psql_path=args.psql,
        db_host=args.db_host,
        db_port=args.db_port,
        db_user=args.db_user,
        db_name=args.db_name,
    )


def run(args: argparse.Namespace) -> int:
    config = _build_config(args)
    database = DatabaseReader(config)
    if args.rollback and args.resume:
        raise BackfillError("--rollback und --resume können nicht kombiniert werden")
    if args.reprocess and args.resume:
        raise BackfillError("--reprocess und --resume können nicht kombiniert werden")
    if args.reprocess and args.rollback:
        raise BackfillError("--reprocess und --rollback können nicht kombiniert werden")

    inventory = []
    ocr = None
    if not args.rollback:
        inventory = database.inventory(
            patient_number=args.patient,
            object_id=args.object_id,
            limit=args.limit,
        )
        print(f"Inventar: {len(inventory)} PDF-Kandidaten")
        if not inventory:
            return 0

        raw_ocr_command = args.ocr_command or os.environ.get("KIENZLEDOKU_OCR_COMMAND", "")
        try:
            if raw_ocr_command.strip():
                if args.force_rotate_page:
                    raise BackfillError(
                        "--force-rotate-page ist nur mit dem OCRmyPDF-Standardbackend verfügbar"
                    )
                ocr_command = shlex.split(raw_ocr_command)
                ocr = CommandOcrBackend(ocr_command, timeout=args.ocr_timeout)
            else:
                ocr = OcrmypdfBackend(
                    ocrmypdf=args.ocrmypdf,
                    pdftotext=args.pdftotext,
                    pdftoppm=args.pdftoppm,
                    qpdf=args.qpdf,
                    tesseract=args.tesseract,
                    language=args.ocr_language,
                    jobs=args.ocr_jobs,
                    timeout=args.ocr_timeout,
                    tesseract_timeout=args.tesseract_timeout,
                    rotate_pages_threshold=args.rotate_pages_threshold,
                    forced_page_rotations=args.force_rotate_page,
                    auto_orient_pages=args.auto_orient_pages,
                    orientation_min_confidence=args.orientation_confidence,
                    progress=print,
                )
        except ValueError as exc:
            raise BackfillError(str(exc)) from exc

    username, password = _credentials(args)
    ssl_context = make_ssl_context(config.insecure, config.ca_cert)
    if config.insecure:
        print("WARNUNG: TLS-Zertifikatsprüfung ist deaktiviert.", file=sys.stderr)
    if args.rollback and not args.apply:
        print("ROLLBACK-DRY-RUN: Revision und Text-Hash werden geprüft; keine Updates.")
    elif args.rollback:
        print("ROLLBACK-APPLY: Nur konfliktfreie Journal-Updates werden zurückgesetzt.")
    elif not args.apply:
        print("DRY-RUN: Es werden keine APS-Updates ausgeführt.")
    else:
        print("APPLY: APS-Texte werden nach OCR und Write-ahead-Journal aktualisiert.")

    http = HttpClient(
        username,
        password,
        ssl_context,
        timeout=config.timeout,
        user_agent=f"kienzledoku-ocr/{__version__}",
    )
    with Journal(args.journal) as journal:
        aps = ApsClient(http, config.aps_base_url)
        if args.rollback:
            summary = RollbackProcessor(
                database, aps, journal, report=print
            ).run(
                apply=args.apply,
                patient_number=args.patient,
                object_id=args.object_id,
                limit=args.limit,
            )
        else:
            assert ocr is not None
            handler = DocumentReferenceHandler(
                database,
                CdnClient(http, config.cdn_base_url),
                aps,
                ocr,
                journal,
                report=print,
                reprocess_existing=args.reprocess,
            )
            summary = BackfillProcessor(handler, journal, report=print).run(
                inventory,
                apply=args.apply,
                resume=args.resume,
            )

    print("Zusammenfassung: " + json.dumps(summary.counts, ensure_ascii=False, sort_keys=True))
    failure_statuses = {
        "missing_cdn",
        "unsupported_type",
        "download_failed",
        "ocr_failed",
        "ocr_empty",
        "aps_find_failed",
        "aps_update_failed",
        "verification_failed",
        "reprocess_conflict",
        "internal_error",
        "rollback_conflict",
        "rollback_failed",
    }
    return 2 if failure_statuses.intersection(summary.counts) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except (BackfillError, RuntimeError, OSError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Abgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
