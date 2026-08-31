"""Safe shell-free adapter for the OCR command selected by the operator."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

from ..errors import OcrError
from .base import OcrBackend


class CommandOcrBackend(OcrBackend):
    """
    Run an explicitly configured command without a shell.

    ``{input}`` is mandatory. If ``{output}`` is present, UTF-8 text is read
    from that file; otherwise stdout is used. ``{mime_type}`` is optional.
    """

    def __init__(self, command: Sequence[str], *, timeout: float = 1800.0) -> None:
        if not command:
            raise ValueError("OCR-Befehl fehlt")
        if not any("{input}" in part for part in command):
            raise ValueError("OCR-Befehl muss den Platzhalter {input} enthalten")
        self._command = tuple(command)
        self._timeout = timeout

    def extract_text(self, path: Path, mime_type: str) -> str:
        descriptor, raw_output_path = tempfile.mkstemp(prefix="kienzledoku-ocr-", suffix=".txt")
        os.close(descriptor)
        output_path = Path(raw_output_path)
        try:
            uses_output = any("{output}" in part for part in self._command)
            command = [
                part.replace("{input}", str(path))
                .replace("{output}", str(output_path))
                .replace("{mime_type}", mime_type)
                for part in self._command
            ]
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=self._timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise OcrError(
                    f"OCR-Befehl überschritt das Zeitlimit von {self._timeout:g} Sekunden"
                ) from exc
            except OSError as exc:
                raise OcrError(f"OCR-Befehl konnte nicht gestartet werden: {exc}") from exc

            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
                suffix = f": {detail[-1][:500]}" if detail else ""
                raise OcrError(
                    f"OCR-Befehl endete mit Status {completed.returncode}{suffix}"
                )

            raw = output_path.read_bytes() if uses_output else completed.stdout
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OcrError("OCR-Ausgabe ist nicht UTF-8-kodiert") from exc
        finally:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
