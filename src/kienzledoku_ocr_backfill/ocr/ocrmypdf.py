"""OCRmyPDF/Tesseract backend matching the confirmed KienzleFax pipeline."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from ..errors import OcrError
from .base import OcrBackend


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
        language: str = "deu+eng",
        jobs: int = 2,
        timeout: float = 1800.0,
        tesseract_timeout: float = 300.0,
    ) -> None:
        if jobs < 1:
            raise ValueError("OCR-Jobs muss mindestens 1 sein")
        if timeout <= 0 or tesseract_timeout <= 0:
            raise ValueError("OCR-Timeouts müssen größer als 0 sein")
        if not language.strip():
            raise ValueError("OCR-Sprache darf nicht leer sein")
        self._ocrmypdf = ocrmypdf
        self._pdftotext = pdftotext
        self._language = language
        self._jobs = jobs
        self._timeout = timeout
        self._tesseract_timeout = tesseract_timeout
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

    def extract_text(self, path: Path, mime_type: str) -> str:
        if mime_type.split(";", 1)[0].strip().lower() not in {
            "application/pdf",
            "application/x-pdf",
        }:
            raise OcrError(f"OCRmyPDF-Backend unterstützt keinen MIME-Typ {mime_type}")

        with tempfile.TemporaryDirectory(prefix="kienzledoku-ocrmypdf-") as tmp:
            output_pdf = Path(tmp) / "ocr.pdf"
            command = [
                self._ocrmypdf,
                *self._detect_mode_args(),
                "-l",
                self._language,
                "--tesseract-oem",
                "1",
                "--rotate-pages",
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
                str(path),
                str(output_pdf),
            ]
            ocr_result = _run(command, timeout=self._timeout)
            if ocr_result.returncode != 0 or not output_pdf.is_file() or output_pdf.stat().st_size == 0:
                detail = self._last_error(ocr_result.stderr)
                suffix = f": {detail}" if detail else ""
                raise OcrError(
                    f"OCRmyPDF endete mit Status {ocr_result.returncode}{suffix}"
                )

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
