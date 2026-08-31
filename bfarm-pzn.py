#!/usr/bin/env python3
"""Run the supplied BfArM PZN downloader/resolver from a repository checkout."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from kienzledoku_ocr_backfill.bfarm_pzn import _main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(_main())
