#!/usr/bin/env python3
"""Register the kienzleocr URI for the current Linux desktop user."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kienzledoku_ocr_backfill.fhir_client import safe_url
from kienzledoku_ocr_backfill.fhir_model import valid_id


def exec_arg(value):
    value = str(value)
    if not value.isprintable():
        raise ValueError("Unzulässiges Zeichen im Programmargument.")
    # Desktop Entry string escaping is applied outside Exec quoting.
    for char in ('\\', '"', '$', chr(96)):
        value = value.replace(char, '\\' + char)
    return '"' + value.replace('\\', '\\\\').replace('%', '%%') + '"'


def save(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Freigegebene HTTPS-FHIR-Basis (bei Ersteinrichtung erforderlich)")
    parser.add_argument("--ca-file", type=Path, help="PEM-Zertifizierungsstelle des T2med-Servers")
    parser.add_argument("--client-id", help="T2med OAuth Client-ID für Geräteanmeldung beim Schreiblauf")
    parser.add_argument("--change-key", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Den Handler für einen echten Schreiblauf registrieren")
    parser.add_argument("--limit", type=int, help="Für einen begrenzten Testlauf")
    parser.add_argument("--patient", help="Optionale FHIR-Patienten-ID zum Testen")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("Linux-Desktop erforderlich")
    if (args.limit is not None and args.limit < 1) or (args.patient and not valid_id(args.patient)):
        parser.error("Testlimit oder Patienten-ID ist ungültig")
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kienzleocr/config.json"
    desktop = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications/kienzleocr.desktop"
    cfg = json.loads(config.read_text()) if config.is_file() else {}
    base = (args.base_url or cfg.get("base_url") or "").rstrip("/")
    parsed = safe_url(base)
    if parsed.path != "/aps/fhir/api/r4" or parsed.query:
        parser.error("Vollständige HTTPS-FHIR-Basis mit /aps/fhir/api/r4 erforderlich")
    if cfg.get("base_url", base) != base:
        cfg = {}
    cfg["base_url"] = base
    if args.ca_file:
        cfg["ca_file"] = str(args.ca_file.resolve(strict=True))
    if args.client_id:
        cfg["client_id"] = args.client_id
    if args.change_key or not cfg.get("api_key"):
        cfg["api_key"] = getpass.getpass("Privater T2med-API-Key: ").strip()
    if not cfg["api_key"]:
        parser.error("Ein API-Key ist erforderlich")
    save(config, json.dumps(cfg, indent=2) + "\n")
    cmd = [sys.executable, ROOT / "kienzledoku-ocr.py", "fhir", "--desktop"]
    if args.apply:
        cmd.append("--apply")
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])
    if args.patient:
        cmd.extend(["--patient", args.patient])
    command = " ".join(exec_arg(v) for v in cmd)
    save(desktop, "[Desktop Entry]\nType=Application\nName=KienzleOCR\nTerminal=true\n"
         f"NoDisplay=true\nMimeType=x-scheme-handler/kienzleocr;\nExec={command} -- %u\n")
    subprocess.run(["xdg-mime", "default", desktop.name, "x-scheme-handler/kienzleocr"], check=True)
    print("kienzleocr:// registriert (" + ("APPLY" if args.apply else "Dry-Run") + ").")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("Einrichtung fehlgeschlagen: " + str(exc))
