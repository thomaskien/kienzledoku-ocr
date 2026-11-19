#!/usr/bin/env python3
"""Inspect OCR before writing anything to T2med."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kienzledoku_ocr_backfill.commander import CommanderCleaner
from kienzledoku_ocr_backfill.formatter import BLOCK_BEGIN, BLOCK_END
from kienzledoku_ocr_backfill.ocr import OcrmypdfBackend
from kienzledoku_ocr_backfill.progress import TimestampedReporter


def clean_export(text, cleaner):
    # Preserve provenance and machine-generated BMP sections from an existing OCR export.
    import re
    tokens = re.split(r"(?m)^(----- BEGINN BUNDESMEDIKATIONSPLAN -----[\s\S]*?^----- ENDE BUNDESMEDIKATIONSPLAN -----|"
                      + re.escape(BLOCK_BEGIN) + "|" + re.escape(BLOCK_END)
                      + r"|KienzleOCR-Quelle [a-f0-9]{64}|kienzledoku OCR v[^\n]+)$", text)
    return "".join(part if i % 2 else cleaner.clean_page(part, 1) for i, part in enumerate(tokens))


def main():
    cfg_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    p = argparse.ArgumentParser(description="PDF oder vorhandene OCR-Textausgabe vor der FHIR-Anlage ansehen.")
    p.add_argument("quelle", type=Path)
    p.add_argument("--config", type=Path, default=cfg_dir / "kienzleocr/config.json")
    args = p.parse_args()
    if args.config.stat().st_mode & 0o077:
        p.error("Konfiguration mit chmod 600 schützen.")
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    progress = TimestampedReporter(sink=lambda line: print(line, file=sys.stderr))
    cleaner = CommanderCleaner(cfg["commander_url"], progress=progress) if cfg.get("commander_url") else None
    if args.quelle.suffix.lower() == ".txt":
        if not cleaner:
            p.error("Für eine Textbereinigung den Commander konfigurieren.")
        text = clean_export(args.quelle.read_text(encoding="utf-8"), cleaner)
    else:
        backend = OcrmypdfBackend(keep_bmp_without_amdb=True, amdb_remote=cfg.get("amdb_remote"),
                                  clean_page=cleaner.clean_page if cleaner else None, progress=progress)
        text = backend.extract_text(args.quelle, "application/pdf")
    print(text)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        sys.exit(f"Vorschau fehlgeschlagen: {exc}")
