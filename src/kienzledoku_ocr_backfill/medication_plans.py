"""Detect and format BMP Data-Matrix payloads without performing OCR."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .bmp import BmpParseError, format_bmp, parse_bmp
from .qr_extractor import extract_qr_codes
from .t2med_amdb import (
    DEFAULT_CLIENT,
    DEFAULT_CONFIG,
    DEFAULT_SOCKET,
    T2medAmdbResolver,
)


@dataclass(frozen=True)
class MedicationPlanScan:
    pages: dict[int, str]
    page_count: int
    diagnostics: dict[str, Any]


class MedicationPlanScanner:
    def __init__(
        self,
        *,
        amdb_config: Path = DEFAULT_CONFIG,
        amdb_client: Path = DEFAULT_CLIENT,
        amdb_socket: Path = DEFAULT_SOCKET,
        amdb_timeout: float = 30.0,
        pdftoppm: str = "pdftoppm",
        dpi: int = 300,
        retry_dpi: int = 600,
        timeout: float = 300.0,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._amdb_config = amdb_config
        self._amdb_client = amdb_client
        self._amdb_socket = amdb_socket
        self._amdb_timeout = amdb_timeout
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
            "amdbConfig": str(self._amdb_config),
            "amdbClient": str(self._amdb_client),
            "amdbSocket": str(self._amdb_socket),
            "amdbAvailable": False,
            "amdbSchema": None,
            "amdbServerVersion": None,
        }
        parsed_plans: list[tuple[Any, Any]] = []
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
            if plan is not None:
                parsed_plans.append((code, plan))

        resolver: Optional[T2medAmdbResolver] = None
        if parsed_plans:
            try:
                resolver = T2medAmdbResolver(
                    config_path=self._amdb_config,
                    client_path=self._amdb_client,
                    socket_path=self._amdb_socket,
                    timeout=self._amdb_timeout,
                    progress=self._progress,
                )
                metadata = resolver.metadata()
                diagnostics["amdbAvailable"] = True
                diagnostics["amdbSchema"] = metadata.get("schema")
                diagnostics["amdbServerVersion"] = metadata.get("serverVersion")
            except Exception as exc:
                diagnostics["errors"].append(
                    {"page": None, "stage": "t2med_amdb", "error": str(exc)[:1000]}
                )
                self._progress(f"T2med-Arzneimitteldatenbank nicht verfügbar: {exc}")
                if resolver is not None:
                    resolver.close()
                resolver = None

        pages: dict[int, list[str]] = {}
        try:
            for code, plan in parsed_plans:
                try:
                    formatted = format_bmp(plan, resolver)
                except Exception as exc:
                    diagnostics["errors"].append(
                        {
                            "page": code.page,
                            "stage": "t2med_amdb_lookup",
                            "error": str(exc)[:1000],
                        }
                    )
                    self._progress(
                        f"T2med-AMDB-Abfrage für Medikationsplan auf Seite "
                        f"{code.page} fehlgeschlagen; daher normale OCR: {exc}"
                    )
                    continue
                if resolver is None and formatted.pzns:
                    diagnostics["errors"].append(
                        {
                            "page": code.page,
                            "stage": "t2med_amdb",
                            "error": (
                                "Medikationsplan erkannt, aber T2med-AMDB ist nicht "
                                "verfügbar; Seite bleibt in der normalen OCR"
                            ),
                        }
                    )
                    self._progress(
                        f"Medikationsplan erkannt: Seite {code.page}; "
                        "T2med-AMDB nicht verfügbar, daher normale OCR"
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
