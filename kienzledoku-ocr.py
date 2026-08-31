#!/usr/bin/env python3
"""Run directly from a repository checkout without installing a package."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from kienzledoku_ocr_backfill.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
