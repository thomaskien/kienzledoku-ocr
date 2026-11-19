#!/usr/bin/env python3
"""Register the kienzleocr URI for the current Linux desktop user."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kienzledoku_ocr_backfill.commander import commander_root
from kienzledoku_ocr_backfill.amdb_remote import load_amdb_profile
from kienzledoku_ocr_backfill.fhir_client import safe_url
from kienzledoku_ocr_backfill.fhir_model import valid_id
from kienzledoku_ocr_backfill.fhir_tls import load_tls


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
    parser.add_argument("--update-existing", action="store_true",
                        help="Vorhandenen Desktopstarter mit allen Laufoptionen auf diese Version umstellen")
    parser.add_argument("--base-url", help="Freigegebene HTTPS-FHIR-Basis (bei Ersteinrichtung erforderlich)")
    commander = parser.add_mutually_exclusive_group()
    commander.add_argument("--commander-url", help="Basisadresse des Praxis-Commanders für die OCR-Bereinigung")
    commander.add_argument("--no-commander", action="store_true", help="Commander-Bereinigung ausschalten")
    amdb = parser.add_mutually_exclusive_group()
    amdb.add_argument("--amdb-profile", type=Path, help="HTTPS-Arbeitsplatzprofil des PZN-Dienstes importieren")
    amdb.add_argument("--local-amdb", action="store_true", help="Wieder die lokale AMDB verwenden")
    trust = parser.add_mutually_exclusive_group()
    trust.add_argument("--ca-file", type=Path, help="PEM-Zertifizierungsstelle des T2med-Servers")
    trust.add_argument("--server-certificate", type=Path,
                       help="PEM-Serverzertifikat, dessen SHA-256-Fingerabdruck im T2med-Client geprüft wurde")
    parser.add_argument("--client-id", help="T2med OAuth Client-ID für Geräteanmeldung und Token-Erneuerung")
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
    updated_desktop = None
    if args.update_existing:
        if args.apply or args.limit or args.patient or args.base_url or not config.is_file() or not desktop.is_file():
            parser.error("--update-existing benötigt den vorhandenen Starter und die vorhandene Konfiguration ohne neue Laufoptionen.")
        previous = desktop.read_text(encoding="utf-8")
        pattern = r'(?m)^(Exec="(?:[^"\\]|\\.)+" )"(?:[^"\\]|\\.)*/kienzledoku-ocr\.py"(?= "fhir" "--desktop" )'
        updated_desktop, hits = re.subn(pattern, lambda m: m[1] + exec_arg(ROOT / "kienzledoku-ocr.py"), previous)
        if hits != 1:
            parser.error("Vorhandener kienzleOCR-Starter nicht eindeutig erkannt.")
    cfg = json.loads(config.read_text()) if config.is_file() else {}
    base = (args.base_url or cfg.get("base_url") or "").rstrip("/")
    parsed = safe_url(base)
    if parsed.path != "/aps/fhir/api/r4" or parsed.query:
        parser.error("Vollständige HTTPS-FHIR-Basis mit /aps/fhir/api/r4 erforderlich")
    if cfg.get("base_url", base).rstrip("/") != base:
        cfg = {}
    if not args.update_existing:
        cfg["base_url"] = base
    if args.commander_url:
        cfg["commander_url"] = commander_root(args.commander_url)
    elif args.no_commander:
        cfg.pop("commander_url", None)
    if args.amdb_profile:
        cfg["amdb_remote"] = load_amdb_profile(args.amdb_profile)
    elif args.local_amdb:
        cfg.pop("amdb_remote", None)
    if args.ca_file:
        cfg["ca_file"] = str(args.ca_file.resolve(strict=True))
        cfg.pop("server_certificate", None)
    if args.server_certificate:
        cfg["server_certificate"] = str(args.server_certificate.resolve(strict=True))
        cfg.pop("ca_file", None)
    load_tls(cfg)
    if args.client_id:
        cfg["client_id"] = args.client_id
    if args.change_key or not cfg.get("api_key"):
        cfg["api_key"] = getpass.getpass("Privater T2med-API-Key: ").strip()
    if not cfg["api_key"]:
        parser.error("Ein API-Key ist erforderlich")
    save(config, json.dumps(cfg, indent=2) + "\n")
    if updated_desktop is not None:
        save(desktop, updated_desktop)
        print("Vorhandener Starter auf neue Programmversion umgestellt. Bisherige Laufoptionen übernommen.")
        return
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
