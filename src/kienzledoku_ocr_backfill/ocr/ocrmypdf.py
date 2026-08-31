"""OCRmyPDF/Tesseract backend matching the confirmed KienzleFax pipeline."""

from __future__ import annotations

import csv
import io
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..errors import OcrError
from ..medication_plans import MedicationPlanScan, MedicationPlanScanner
from .base import OcrBackend


ORIENTATION_ANCHORS = (
    "medikationsplan",
    "wirkstoff",
    "handelsname",
    "stärke",
    "einnahme",
    "morgens",
    "mittags",
    "abends",
    "tabletten",
    "patient",
    "arzt",
    "datum",
)


@dataclass(frozen=True)
class OrientationDecision:
    page: int
    rotation: str
    confidence: Optional[float]
    method: str
    status: str
    scores: Optional[dict[str, Any]] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "rotation": self.rotation,
            "confidence": self.confidence,
            "method": self.method,
            "status": self.status,
            "scores": self.scores,
        }


class _Result:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run(command: Sequence[str], *, timeout: float) -> _Result:
    """Run without a shell and terminate the complete OCR process group on timeout."""
    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise OcrError(f"Programm konnte nicht gestartet werden: {command[0]}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise OcrError(
            f"Programm überschritt das Zeitlimit von {timeout:g} Sekunden: {command[0]}"
        ) from exc
    return _Result(process.returncode, stdout, stderr)


class OcrmypdfBackend(OcrBackend):
    """Create a temporary searchable PDF, then extract its complete UTF-8 text."""

    def __init__(
        self,
        *,
        ocrmypdf: str = "ocrmypdf",
        pdftotext: str = "pdftotext",
        pdftoppm: str = "pdftoppm",
        qpdf: str = "qpdf",
        tesseract: str = "tesseract",
        language: str = "deu+eng",
        jobs: int = 2,
        timeout: float = 1800.0,
        tesseract_timeout: float = 300.0,
        rotate_pages_threshold: float = 14.0,
        forced_page_rotations: Sequence[tuple[int, str]] = (),
        auto_orient_pages: bool = True,
        orientation_min_confidence: float = 5.0,
        medication_plan_codes: bool = True,
        pzn_database: Optional[Path] = None,
        barcode_dpi: int = 300,
        barcode_retry_dpi: int = 600,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        if jobs < 1:
            raise ValueError("OCR-Jobs muss mindestens 1 sein")
        if timeout <= 0 or tesseract_timeout <= 0:
            raise ValueError("OCR-Timeouts müssen größer als 0 sein")
        if not language.strip():
            raise ValueError("OCR-Sprache darf nicht leer sein")
        if rotate_pages_threshold < 0:
            raise ValueError("OCR-Drehschwelle darf nicht negativ sein")
        if orientation_min_confidence < 0:
            raise ValueError("Orientierungsschwelle darf nicht negativ sein")
        if barcode_dpi < 72 or barcode_retry_dpi < barcode_dpi:
            raise ValueError("Ungültige Data-Matrix-Renderauflösung")
        for page, angle in forced_page_rotations:
            if page < 1 or angle not in {"+90", "-90", "+180", "-180", "+270", "-270"}:
                raise ValueError("Ungültige erzwungene Seitendrehung")
        self._ocrmypdf = ocrmypdf
        self._pdftotext = pdftotext
        self._pdftoppm = pdftoppm
        self._qpdf = qpdf
        self._tesseract = tesseract
        self._language = language
        self._jobs = jobs
        self._timeout = timeout
        self._tesseract_timeout = tesseract_timeout
        self._rotate_pages_threshold = rotate_pages_threshold
        self._forced_page_rotations = tuple(forced_page_rotations)
        self._auto_orient_pages = auto_orient_pages
        self._orientation_min_confidence = orientation_min_confidence
        self._medication_plan_codes = medication_plan_codes
        self._pzn_database = pzn_database
        self._barcode_dpi = barcode_dpi
        self._barcode_retry_dpi = barcode_retry_dpi
        self._progress = progress or (lambda message: None)
        self._last_orientation_decisions: list[OrientationDecision] = []
        self._last_medication_diagnostics: Optional[dict[str, Any]] = None
        self._mode_args: Optional[tuple[str, ...]] = None

    def _detect_mode_args(self) -> tuple[str, ...]:
        if self._mode_args is not None:
            return self._mode_args
        result = _run([self._ocrmypdf, "--help"], timeout=min(30.0, self._timeout))
        help_text = (result.stdout + result.stderr).decode("utf-8", "replace")
        if result.returncode != 0:
            raise OcrError(
                f"OCRmyPDF-Hilfe endete mit Status {result.returncode}"
            )
        self._mode_args = ("--mode", "skip") if "--mode" in help_text else ("--skip-text",)
        return self._mode_args

    @staticmethod
    def _last_error(stderr: bytes) -> str:
        lines = stderr.decode("utf-8", "replace").strip().splitlines()
        return lines[-1][:1000] if lines else ""

    @staticmethod
    def _parse_osd(result: _Result) -> tuple[int, float]:
        text = (result.stdout + result.stderr).decode("utf-8", "replace")
        rotate_match = re.search(r"(?m)^Rotate:\s*(0|90|180|270)\s*$", text)
        confidence_match = re.search(
            r"(?m)^Orientation confidence:\s*([-+]?[0-9]+(?:[.,][0-9]+)?)\s*$",
            text,
        )
        if rotate_match is None or confidence_match is None:
            raise ValueError("Tesseract-OSD-Ausgabe ist unvollständig")
        return (
            int(rotate_match.group(1)),
            float(confidence_match.group(1).replace(",", ".")),
        )

    @staticmethod
    def _score_tsv(raw: bytes) -> dict[str, Any]:
        text = raw.decode("utf-8", "replace")
        words: list[str] = []
        confidences: list[float] = []
        try:
            rows = csv.DictReader(io.StringIO(text), delimiter="\t")
            for row in rows:
                word = (row.get("text") or "").strip()
                if not word:
                    continue
                try:
                    confidence = float((row.get("conf") or "-1").replace(",", "."))
                except ValueError:
                    continue
                if confidence < 0:
                    continue
                words.append(word)
                confidences.append(confidence)
        except csv.Error:
            words = []
            confidences = []

        joined = " ".join(words).casefold()
        anchors = [anchor for anchor in ORIENTATION_ANCHORS if anchor in joined]
        plausible = [
            word
            for word in words
            if len(re.findall(r"[^\W\d_]", word, flags=re.UNICODE)) >= 2
        ]
        mean_confidence = (
            sum(confidences) / len(confidences) if confidences else 0.0
        )
        plausible_ratio = len(plausible) / len(words) if words else 0.0
        score = (
            mean_confidence
            + min(len(words), 100) * 0.12
            + plausible_ratio * 10.0
            + len(anchors) * 12.0
        )
        return {
            "score": round(score, 2),
            "meanConfidence": round(mean_confidence, 2),
            "words": len(words),
            "anchors": anchors,
        }

    @staticmethod
    def _rotation_value(angle: int) -> str:
        return "0" if angle == 0 else f"+{angle}"

    @staticmethod
    def _page_number(path: Path) -> int:
        match = re.search(r"-([0-9]+)\.png$", path.name)
        if match is None:
            raise OcrError(f"Seitennummer aus Rasterdatei nicht lesbar: {path.name}")
        return int(match.group(1))

    def _render_all_pages(self, path: Path, tmp: Path) -> dict[int, Path]:
        prefix = tmp / "orientation-page"
        result = _run(
            [
                self._pdftoppm,
                "-png",
                "-gray",
                "-r",
                "150",
                str(path),
                str(prefix),
            ],
            timeout=self._timeout,
        )
        if result.returncode != 0:
            detail = self._last_error(result.stderr)
            suffix = f": {detail}" if detail else ""
            raise OcrError(
                f"pdftoppm-Orientierungsprüfung endete mit Status {result.returncode}{suffix}"
            )
        pages = {
            self._page_number(image): image
            for image in sorted(tmp.glob("orientation-page-*.png"))
        }
        if not pages:
            raise OcrError("pdftoppm erzeugte keine Seitenbilder")
        return pages

    def _candidate_image(
        self,
        source_pdf: Path,
        original_image: Path,
        page: int,
        angle: int,
        tmp: Path,
    ) -> Path:
        if angle == 0:
            return original_image
        candidate_pdf = tmp / f"orientation-{page}-{angle}.pdf"
        rotate_result = _run(
            [
                self._qpdf,
                str(source_pdf),
                str(candidate_pdf),
                f"--rotate=+{angle}:{page}",
                "--flatten-rotation",
            ],
            timeout=self._timeout,
        )
        if rotate_result.returncode != 0:
            raise OcrError(
                f"qpdf-Kandidat Seite {page} / {angle}° fehlgeschlagen"
            )
        prefix = tmp / f"orientation-{page}-{angle}"
        render_result = _run(
            [
                self._pdftoppm,
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-png",
                "-gray",
                "-r",
                "150",
                str(candidate_pdf),
                str(prefix),
            ],
            timeout=self._timeout,
        )
        image = prefix.with_suffix(".png")
        if render_result.returncode != 0 or not image.is_file():
            raise OcrError(
                f"pdftoppm-Kandidat Seite {page} / {angle}° fehlgeschlagen"
            )
        return image

    def _four_way_decision(
        self,
        source_pdf: Path,
        original_image: Path,
        page: int,
        osd_confidence: Optional[float],
        tmp: Path,
    ) -> OrientationDecision:
        candidate_scores: dict[str, Any] = {}
        for angle in (0, 90, 180, 270):
            image = self._candidate_image(
                source_pdf, original_image, page, angle, tmp
            )
            result = _run(
                [
                    self._tesseract,
                    str(image),
                    "stdout",
                    "-l",
                    self._language,
                    "--psm",
                    "3",
                    "tsv",
                ],
                timeout=min(self._tesseract_timeout, self._timeout),
            )
            score = self._score_tsv(result.stdout) if result.returncode == 0 else {
                "score": 0.0,
                "meanConfidence": 0.0,
                "words": 0,
                "anchors": [],
            }
            candidate_scores[str(angle)] = score

        ranked = sorted(
            ((details["score"], int(angle)) for angle, details in candidate_scores.items()),
            reverse=True,
        )
        best_score, best_angle = ranked[0]
        second_score = ranked[1][0]
        margin = best_score - second_score
        score_details = {
            "candidates": candidate_scores,
            "margin": round(margin, 2),
        }
        if best_score >= 25.0 and margin >= 4.0:
            return OrientationDecision(
                page=page,
                rotation=self._rotation_value(best_angle),
                confidence=osd_confidence,
                method="four_way",
                status="rotated" if best_angle else "kept",
                scores=score_details,
            )
        return OrientationDecision(
            page=page,
            rotation="0",
            confidence=osd_confidence,
            method="four_way",
            status="uncertain",
            scores=score_details,
        )

    def analyze_page_orientations(
        self, path: Path, tmp: Path
    ) -> list[OrientationDecision]:
        manual = {
            page: angle for page, angle in self._forced_page_rotations
        }
        if not self._auto_orient_pages:
            return [
                OrientationDecision(
                    page=page,
                    rotation=angle,
                    confidence=None,
                    method="manual",
                    status="rotated",
                )
                for page, angle in sorted(manual.items())
            ]

        pages = self._render_all_pages(path, tmp)
        missing_manual_pages = sorted(set(manual) - set(pages))
        if missing_manual_pages:
            raise OcrError(
                "Erzwungene Drehung verweist auf nicht vorhandene Seite(n): "
                + ", ".join(str(page) for page in missing_manual_pages)
            )
        self._progress(f"Orientierungsprüfung: {len(pages)} Seite(n)")
        decisions: list[OrientationDecision] = []
        for page, image in sorted(pages.items()):
            if page in manual:
                decision = OrientationDecision(
                    page=page,
                    rotation=manual[page],
                    confidence=None,
                    method="manual",
                    status="rotated",
                )
            else:
                osd_result = _run(
                    [
                        self._tesseract,
                        str(image),
                        "stdout",
                        "-l",
                        "osd",
                        "--psm",
                        "0",
                    ],
                    timeout=min(self._tesseract_timeout, self._timeout),
                )
                osd_rotation: Optional[int] = None
                osd_confidence: Optional[float] = None
                if osd_result.returncode == 0:
                    try:
                        osd_rotation, osd_confidence = self._parse_osd(osd_result)
                    except ValueError:
                        pass
                if (
                    osd_rotation is not None
                    and osd_confidence is not None
                    and osd_confidence >= self._orientation_min_confidence
                ):
                    decision = OrientationDecision(
                        page=page,
                        rotation=self._rotation_value(osd_rotation),
                        confidence=round(osd_confidence, 2),
                        method="osd",
                        status="rotated" if osd_rotation else "kept",
                    )
                else:
                    decision = self._four_way_decision(
                        path,
                        image,
                        page,
                        osd_confidence,
                        tmp,
                    )
            decisions.append(decision)
            if decision.method == "manual":
                self._progress(
                    f"Orientierung Seite {page}: manuell {decision.rotation}°"
                )
            elif decision.status == "uncertain":
                self._progress(
                    f"Orientierung Seite {page}: unsicher, bleibt bei 0°"
                )
            else:
                confidence = (
                    f", Konfidenz {decision.confidence:g}"
                    if decision.confidence is not None
                    else ""
                )
                self._progress(
                    f"Orientierung Seite {page}: {decision.rotation}° "
                    f"({decision.method}{confidence})"
                )
        return decisions

    def _prepare_ocr_input(self, path: Path, tmp: Path) -> Path:
        decisions = self.analyze_page_orientations(path, tmp)
        self._last_orientation_decisions = decisions
        rotations = [
            f"--rotate={decision.rotation}:{decision.page}"
            for decision in decisions
            if decision.rotation != "0"
        ]
        if not rotations:
            return path
        rotated_input = tmp / "oriented.pdf"
        rotate_result = _run(
            [
                self._qpdf,
                str(path),
                str(rotated_input),
                *rotations,
                "--flatten-rotation",
            ],
            timeout=self._timeout,
        )
        if (
            rotate_result.returncode != 0
            or not rotated_input.is_file()
            or rotated_input.stat().st_size == 0
        ):
            detail = self._last_error(rotate_result.stderr)
            suffix = f": {detail}" if detail else ""
            raise OcrError(
                "qpdf-Seitendrehung endete mit Status "
                f"{rotate_result.returncode}{suffix}"
            )
        return rotated_input

    def diagnostics(self) -> dict[str, Any]:
        return {
            "pageOrientations": [
                decision.as_dict() for decision in self._last_orientation_decisions
            ],
            "medicationPlans": self._last_medication_diagnostics,
        }

    def _scan_medication_plans(self, path: Path) -> MedicationPlanScan:
        if not self._medication_plan_codes:
            return MedicationPlanScan({}, 0, {"disabled": True})
        scanner = MedicationPlanScanner(
            pzn_database=self._pzn_database,
            pdftoppm=self._pdftoppm,
            dpi=self._barcode_dpi,
            retry_dpi=self._barcode_retry_dpi,
            timeout=min(self._tesseract_timeout, self._timeout),
            progress=self._progress,
        )
        return scanner.scan(path)

    def _extract_page_text(self, output_pdf: Path, page: int) -> str:
        result = _run(
            [
                self._pdftotext,
                "-f",
                str(page),
                "-l",
                str(page),
                "-enc",
                "UTF-8",
                "-nopgbrk",
                str(output_pdf),
                "-",
            ],
            timeout=self._timeout,
        )
        if result.returncode != 0:
            detail = self._last_error(result.stderr)
            suffix = f": {detail}" if detail else ""
            raise OcrError(
                f"pdftotext für Seite {page} endete mit Status "
                f"{result.returncode}{suffix}"
            )
        try:
            return result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise OcrError(
                f"pdftotext-Ausgabe für Seite {page} ist nicht UTF-8-kodiert"
            ) from exc

    def _mixed_text(
        self, output_pdf: Path, medication_scan: MedicationPlanScan
    ) -> str:
        parts: list[str] = []
        for page in range(1, medication_scan.page_count + 1):
            if page in medication_scan.pages:
                text = medication_scan.pages[page]
            else:
                text = self._extract_page_text(output_pdf, page)
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def extract_text(self, path: Path, mime_type: str) -> str:
        self._last_orientation_decisions = []
        self._last_medication_diagnostics = None
        if mime_type.split(";", 1)[0].strip().lower() not in {
            "application/pdf",
            "application/x-pdf",
        }:
            raise OcrError(f"OCRmyPDF-Backend unterstützt keinen MIME-Typ {mime_type}")

        with tempfile.TemporaryDirectory(prefix="kienzledoku-ocrmypdf-") as tmp:
            tmp_path = Path(tmp)
            ocr_input = self._prepare_ocr_input(path, tmp_path)
            medication_scan = self._scan_medication_plans(ocr_input)
            self._last_medication_diagnostics = medication_scan.diagnostics
            medication_pages = set(medication_scan.pages)
            ocr_pages = [
                page
                for page in range(1, medication_scan.page_count + 1)
                if page not in medication_pages
            ]

            if medication_pages and not ocr_pages:
                return "\n\n".join(
                    medication_scan.pages[page] for page in sorted(medication_pages)
                )

            output_pdf = Path(tmp) / "ocr.pdf"
            command = [
                self._ocrmypdf,
                *self._detect_mode_args(),
                "-l",
                self._language,
                "--tesseract-oem",
                "1",
                "--rotate-pages",
                "--rotate-pages-threshold",
                f"{self._rotate_pages_threshold:g}",
                "--deskew",
                "--clean",
                "--oversample",
                "300",
                "--output-type",
                "pdfa-3",
                "--optimize",
                "1",
                "--tesseract-timeout",
                f"{self._tesseract_timeout:g}",
                "--jobs",
                str(self._jobs),
            ]
            if medication_pages:
                command.extend(["--pages", ",".join(str(page) for page in ocr_pages)])
            command.extend([str(ocr_input), str(output_pdf)])
            ocr_result = _run(command, timeout=self._timeout)
            if ocr_result.returncode != 0 or not output_pdf.is_file() or output_pdf.stat().st_size == 0:
                detail = self._last_error(ocr_result.stderr)
                suffix = f": {detail}" if detail else ""
                raise OcrError(
                    f"OCRmyPDF endete mit Status {ocr_result.returncode}{suffix}"
                )

            if medication_pages:
                return self._mixed_text(output_pdf, medication_scan)

            text_result = _run(
                [
                    self._pdftotext,
                    "-enc",
                    "UTF-8",
                    "-nopgbrk",
                    str(output_pdf),
                    "-",
                ],
                timeout=self._timeout,
            )
            if text_result.returncode != 0:
                detail = self._last_error(text_result.stderr)
                suffix = f": {detail}" if detail else ""
                raise OcrError(
                    f"pdftotext endete mit Status {text_result.returncode}{suffix}"
                )
            try:
                return text_result.stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OcrError("pdftotext-Ausgabe ist nicht UTF-8-kodiert") from exc
