"""Read-only inventory and revision lookup through T2med's bundled psql."""

from __future__ import annotations

import csv
import io
import re
import subprocess
from pathlib import Path
from typing import Optional

from .config import T2medConfig
from .errors import DatabaseReadError
from .models import InventoryItem


_SAFE_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def is_pdf_file_info(filename: Optional[str], mime_type: Optional[str]) -> bool:
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    name = (filename or "").strip().lower()
    return mime in {"application/pdf", "application/x-pdf"} or name.endswith(".pdf")

INVENTORY_SQL = r"""
COPY (
    WITH inventory AS (
        SELECT DISTINCT ON (v.objectid)
            p.nummer::text AS patientennummer,
            v.objectid,
            v.revision,
            v.classid,
            v.gueltigkeitszeitpunkt::text AS gueltigkeitszeitpunkt,
            v.verweis,
            fi.name,
            fi.mimetype,
            fi.groesse
        FROM aps.verweiseintrag AS v
        JOIN aps.patient AS p
          ON p.objectid = v.patient_objectid
        JOIN aps.verweiseintragdateiinfo AS fi
          ON fi.verweiseintrag_objectid = v.objectid
        WHERE v.verweis LIKE 'cdn://%'
          AND v.classid = 60
        ORDER BY v.objectid, fi.name NULLS LAST
    )
    SELECT
        patientennummer,
        objectid,
        revision,
        classid,
        gueltigkeitszeitpunkt,
        verweis,
        name,
        mimetype,
        groesse
    FROM inventory
    ORDER BY patientennummer, gueltigkeitszeitpunkt, objectid
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)
""".strip()


class DatabaseReader:
    def __init__(self, config: T2medConfig) -> None:
        self._config = config

    def _command(self, sql: str) -> list[str]:
        return [
            str(self._config.psql_path),
            "-w",
            "-h",
            self._config.db_host,
            "-p",
            str(self._config.db_port),
            "-U",
            self._config.db_user,
            "-d",
            self._config.db_name,
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]

    def _run(self, sql: str) -> str:
        psql = Path(self._config.psql_path)
        if not psql.is_file():
            raise DatabaseReadError(f"psql nicht gefunden: {psql}")
        try:
            completed = subprocess.run(
                self._command(sql),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            raise DatabaseReadError(f"psql konnte nicht gestartet werden: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise DatabaseReadError(f"Read-only-Datenbankabfrage fehlgeschlagen{suffix}")
        return completed.stdout

    def inventory(
        self,
        *,
        patient_number: Optional[str] = None,
        object_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[InventoryItem]:
        rows = csv.DictReader(io.StringIO(self._run(INVENTORY_SQL)))
        items: list[InventoryItem] = []
        for row in rows:
            item = InventoryItem(
                patient_number=row["patientennummer"],
                object_id=row["objectid"],
                revision=int(row["revision"]),
                class_id=int(row["classid"]),
                valid_at=row["gueltigkeitszeitpunkt"] or None,
                cdn_reference=row["verweis"],
                filename=row["name"] or None,
                mime_type=row["mimetype"] or None,
                size=int(row["groesse"]) if row["groesse"] else None,
            )
            # --limit counts actual PDF candidates, not other classid-60 files.
            if not is_pdf_file_info(item.filename, item.mime_type):
                continue
            if patient_number is not None and item.patient_number != patient_number:
                continue
            if object_id is not None and item.object_id != object_id:
                continue
            items.append(item)
            if limit is not None and len(items) >= limit:
                break
        return items

    def current_revision(self, object_id: str) -> int:
        if not _SAFE_OBJECT_ID.fullmatch(object_id):
            raise DatabaseReadError("Ungültige objectId für Revision-Abfrage")
        sql = (
            "SELECT revision FROM aps.verweiseintrag "
            f"WHERE objectid = '{object_id}';"
        )
        output = self._run(sql).strip()
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if len(lines) != 1 or not lines[0].isdigit():
            raise DatabaseReadError(
                f"Keine eindeutige aktuelle Revision für objectId {object_id}"
            )
        return int(lines[0])
