"""Append-only, fsync-backed JSONL journal containing rollback material."""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
COMPLETED_STATUSES = {"updated", "already_ocr"}


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None
        self._lock_handle = None

    def __enter__(self) -> "Journal":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self._lock_handle = os.fdopen(lock_descriptor, "w")
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_handle.close()
            self._lock_handle = None
            raise RuntimeError(
                f"Für dieses Journal läuft bereits ein Prozess: {self.path}"
            ) from exc
        self._lock_handle.write(str(os.getpid()))
        self._lock_handle.flush()

        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.chmod(self.path, 0o600)
        self._handle = os.fdopen(descriptor, "a", encoding="utf-8")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._lock_handle is not None:
            self._lock_handle.close()
            self._lock_handle = None

    def write(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            raise RuntimeError("Journal ist nicht geöffnet")
        enriched = {
            "timestamp": datetime.now(BERLIN).isoformat(timespec="seconds"),
            **record,
        }
        json.dump(enriched, self._handle, ensure_ascii=False, separators=(",", ":"))
        self._handle.write("\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def records(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Ungültiges JSONL im Journal, Zeile {line_number}"
                    ) from exc
                if isinstance(value, dict):
                    result.append(value)
        return result

    def completed_object_ids(self) -> set[str]:
        completed: set[str] = set()
        for record in self.records():
            object_id = record.get("objectId")
            if not object_id:
                continue
            object_id = str(object_id)
            if record.get("status") in COMPLETED_STATUSES:
                completed.add(object_id)
            elif record.get("status") == "rolled_back":
                completed.discard(object_id)
        return completed

    def rollback_candidates(self) -> list[dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        for record in self.records():
            object_id = record.get("objectId")
            if not object_id:
                continue
            object_id = str(object_id)
            if record.get("status") == "updated":
                candidates[object_id] = record
            elif record.get("status") == "rolled_back":
                candidates.pop(object_id, None)
        return list(candidates.values())
