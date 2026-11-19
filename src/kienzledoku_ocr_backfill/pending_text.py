"""Private, per-document checkpoints for completed OCR awaiting a verified FHIR write."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .errors import OcrError


class PendingTextError(OcrError):
    """Safe diagnostic without the document contents or its identifier."""


class PendingText:
    def __init__(self, journal, server, profile):
        self.directory = journal.with_name(journal.name + ".pending") / server
        self.profile = profile

    def _path(self, key):
        # key is the SHA-256 of patient, document, attachment index and original file hash.
        return self.directory / key

    def read(self, key, date):
        path = self._path(key)
        try:
            with path.open(encoding="utf-8") as f:
                if os.fstat(f.fileno()).st_mode & 0o077:
                    raise PendingTextError("Ungeschützter OCR-Zwischenspeicher.")
                record = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise PendingTextError("OCR-Zwischenspeicher nicht lesbar.") from None
        if not isinstance(record, dict) or record.get("key") != key:
            raise PendingTextError("OCR-Zwischenspeicher passt nicht zum Dokument.")
        if record.get("date") != date or record.get("profile") != self.profile:
            return None
        text = record.get("text")
        if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != record.get("hash"):
            raise PendingTextError("OCR-Zwischenspeicher beschädigt.")
        return text

    def store(self, key, date, text):
        try:
            self._store(key, date, text)
        except OSError:
            raise PendingTextError("OCR-Zwischenspeicher konnte nicht gesichert werden. Speicherplatz und Rechte prüfen.") from None

    def _store(self, key, date, text):
        self.directory.parent.mkdir(mode=0o700, exist_ok=True)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        record = {"key": key, "date": date, "profile": self.profile, "text": text,
                  "hash": hashlib.sha256(text.encode()).hexdigest()}
        temp = tempfile.NamedTemporaryFile(dir=self.directory, mode="w", encoding="utf-8", delete=False)
        tmp = Path(temp.name)
        try:
            with temp as f:
                json.dump(record, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self._path(key))
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            tmp.unlink(missing_ok=True)

    def discard(self, key):
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            raise PendingTextError("Bestätigter OCR-Zwischenspeicher konnte nicht entfernt werden.") from None
