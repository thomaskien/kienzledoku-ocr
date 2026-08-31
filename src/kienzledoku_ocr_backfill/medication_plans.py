"""Detect and format BMP Data-Matrix payloads without performing OCR."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .bfarm_pzn import PZNResolver
from .bmp import BmpParseError, format_bmp, parse_bmp
from .qr_extractor import extract_qr_codes


@dataclass(frozen=True)
class MedicationPlanScan:
    pages: dict[int, str]
    page_count: int
    diagnostics: dict[str, Any]


class MedicationPlanScanner:
    def __init__(
        self,
        *,
        pzn_database: Optional[Path],
        pdftoppm: str = "pdftoppm",
        dpi: int = 300,
        retry_dpi: int = 600,
        timeout: float = 300.0,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._pzn_database = pzn_database
        self._pdftoppm = pdftoppm
        self._dpi = dpi
        self._retry_dpi = retry_dpi
        self._timeout = timeout
        self._progress = progress or (lambda message: None)

    def scan(self, source: Path) -> MedicationPlanScan:
        self._progress("Data-Matrix-Prüfung läuft")
        extracted = extract_qr_codes(
            source,
            pdftoppm=self._pdftoppm,
            dpi=self._dpi,
            retry_dpi=self._retry_dpi,
            timeout=self._timeout,
        )
        diagnostics: dict[str, Any] = {
            "codesFound": len(extracted.codes),
            "pagesScanned": extracted.pages_scanned,
            "errors": list(extracted.errors),
            "plans": [],
            "pznDatabase": str(self._pzn_database) if self._pzn_database else None,
            "pznDatabaseAvailable": False,
            "pznDatabaseRelease": None,
        }
        resolver: Optional[PZNResolver] = None
        if self._pzn_database and self._pzn_database.is_file():
            try:
                resolver = PZNResolver(self._pzn_database)
                metadata = resolver.metadata()
                diagnostics["pznDatabaseAvailable"] = True
                diagnostics["pznDatabaseRelease"] = metadata.get("release_date")
            except Exception as exc:
                diagnostics["errors"].append(
                    {"page": None, "stage": "pzn_database", "error": str(exc)[:1000]}
                )
                if resolver is not None:
                    resolver.close()
                resolver = None

        pages: dict[int, list[str]] = {}
        try:
            for code in extracted.codes:
                try:
                    plan = parse_bmp(code.data)
                except BmpParseError as exc:
                    diagnostics["errors"].append(
                        {"page": code.page, "stage": "bmp_parse", "error": str(exc)}
                    )
                    self._progress(
                        f"Medikationsplan-Code auf Seite {code.page} ist ungültig: {exc}"
                    )
                    continue
                if plan is None:
                    continue
                formatted = format_bmp(plan, resolver)
                if resolver is None and formatted.pzns:
                    diagnostics["errors"].append(
                        {
                            "page": code.page,
                            "stage": "pzn_database",
                            "error": (
                                "Medikationsplan erkannt, aber PZN-Datenbank ist nicht "
                                "verfügbar; Seite bleibt in der normalen OCR"
                            ),
                        }
                    )
                    self._progress(
                        f"Medikationsplan erkannt: Seite {code.page}; "
                        "PZN-Datenbank fehlt, daher normale OCR"
                    )
                    continue
                pages.setdefault(code.page, []).append(formatted.text)
                plan_diagnostic = {
                    "page": code.page,
                    "type": code.code_type,
                    "rect": code.rect,
                    "retry": code.retry,
                    "dpi": code.dpi,
                    "version": plan.version,
                    "planId": plan.plan_id,
                    "planPage": plan.page,
                    "planTotalPages": plan.total_pages,
                    "pzns": list(formatted.pzns),
                    "unresolvedPzns": list(formatted.unresolved_pzns),
                }
                diagnostics["plans"].append(plan_diagnostic)
                self._progress(
                    f"Medikationsplan erkannt: Seite {code.page} ({code.code_type}); "
                    "strukturierte Ausgabe ersetzt OCR dieser Seite"
                )
        finally:
            if resolver is not None:
                resolver.close()

        return MedicationPlanScan(
            pages={page: "\n\n".join(values) for page, values in pages.items()},
            page_count=extracted.pages_scanned,
            diagnostics=diagnostics,
        )
