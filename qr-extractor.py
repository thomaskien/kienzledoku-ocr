#!/usr/bin/env python3
"""Run the generic QR/Data-Matrix extractor from a repository checkout."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from kienzledoku_ocr_backfill.qr_extractor import _main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_main())
