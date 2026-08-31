"""Read-only PZN resolution against T2med's active local MariaDB AMDB."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .bfarm_pzn import normalize_pzn


DEFAULT_CONFIG = Path("/opt/t2med/server/mmi/service.conf")
DEFAULT_CLIENT = Path("/opt/t2med/server/mariadb/bin/mariadb")
DEFAULT_SOCKET = Path("/var/opt/t2med/data/mariadb/t2med-mariadb")
SCHEMA_RE = re.compile(r"[A-Za-z0-9_]+")


class T2medAmdbError(RuntimeError):
    pass


def read_service_config(path: str | Path) -> dict[str, str]:
    config_path = Path(path)
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise T2medAmdbError(f"T2med-AMDB-Konfiguration nicht lesbar: {config_path}: {exc}") from exc
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


class T2medAmdbResolver:
    """Resolve PZN through MEDPLAN_PACKAGE in a read-only MariaDB transaction."""

    def __init__(
        self,
        *,
        config_path: str | Path = DEFAULT_CONFIG,
        client_path: str | Path = DEFAULT_CLIENT,
        socket_path: str | Path = DEFAULT_SOCKET,
        timeout: float = 30.0,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("T2med-AMDB-Timeout muss größer als 0 sein")
        self.config_path = Path(config_path)
        self.client_path = Path(client_path)
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._progress = progress or (lambda message: None)
        self._metadata: Optional[dict[str, Any]] = None
        self._cache: dict[str, Optional[dict[str, Any]]] = {}

        config = read_service_config(self.config_path)
        schema = config.get("dball.dbschema", "").strip()
        if SCHEMA_RE.fullmatch(schema) is None:
            raise T2medAmdbError(
                f"Ungültiges oder fehlendes dball.dbschema in {self.config_path}"
            )
        if not self.client_path.is_file() or not os.access(self.client_path, os.X_OK):
            raise T2medAmdbError(f"MariaDB-Client nicht ausführbar: {self.client_path}")
        if not self.socket_path.exists():
            raise T2medAmdbError(f"MariaDB-Socket nicht gefunden: {self.socket_path}")
        self.schema = schema

    def _run_select(self, select_sql: str) -> list[str]:
        statement = select_sql.strip().rstrip(";")
        if not statement.upper().startswith("SELECT "):
            raise T2medAmdbError("T2med-AMDB erlaubt ausschließlich SELECT")
        query = f"START TRANSACTION READ ONLY;\n{statement};\nROLLBACK;"
        command = [
            str(self.client_path),
            "--protocol=SOCKET",
            f"--socket={self.socket_path}",
            f"--database={self.schema}",
            "--default-character-set=utf8mb4",
            "--batch",
            "--raw",
            "--silent",
            "--skip-column-names",
            f"--connect-timeout={max(1, round(self.timeout))}",
            "--execute",
            query,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise T2medAmdbError(f"T2med-AMDB-Abfrage konnte nicht ausgeführt werden: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            message = detail[-1] if detail else f"Status {result.returncode}"
            raise T2medAmdbError(f"T2med-AMDB-Abfrage fehlgeschlagen: {message[:1000]}")
        return [line for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _json_row(line: str) -> dict[str, Any]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise T2medAmdbError("T2med-AMDB lieferte ungültiges JSON") from exc
        if not isinstance(value, dict):
            raise T2medAmdbError("T2med-AMDB lieferte kein JSON-Objekt")
        return value

    def metadata(self) -> dict[str, Any]:
        if self._metadata is None:
            self._progress(f"T2med-Arzneimitteldatenbank wird abgefragt: Schema {self.schema}")
            rows = self._run_select(
                "SELECT JSON_OBJECT("
                "'schema', DATABASE(), "
                "'serverVersion', VERSION(), "
                "'sourceTable', 'MEDPLAN_PACKAGE'"
                ") "
                "FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'MEDPLAN_PACKAGE' "
                "AND TABLE_TYPE = 'BASE TABLE'"
            )
            if len(rows) != 1:
                raise T2medAmdbError(
                    "T2med-AMDB-Tabelle MEDPLAN_PACKAGE fehlt oder ist nicht eindeutig"
                )
            self._metadata = self._json_row(rows[0])
            self._progress(
                "T2med-Arzneimitteldatenbank verbunden: "
                f"Schema {self._metadata.get('schema')}, "
                f"MariaDB {self._metadata.get('serverVersion')}"
            )
        return dict(self._metadata)

    def lookup(self, pzn: str | int) -> Optional[dict[str, Any]]:
        normalized = normalize_pzn(pzn)
        if normalized in self._cache:
            self._progress(f"T2med-AMDB: PZN {normalized} wird erneut verwendet")
            return self._cache[normalized]

        self._progress(f"T2med-AMDB: PZN {normalized} wird abgefragt")
        rows = self._run_select(
            "SELECT JSON_OBJECT("
            "'pzn', PZN, "
            "'name', PACKAGENAMEIFA, "
            "'substance', MOLECULENAME, "
            "'strength', MOLECULEMASSES, "
            "'formIfa', PHARMFORMIFACODE, "
            "'formMedicationPlan', MEDPLANPHARMFORMCODE"
            ") "
            "FROM MEDPLAN_PACKAGE "
            f"WHERE PZN = '{normalized}' "
            "LIMIT 2"
        )
        if not rows:
            self._cache[normalized] = None
            self._progress(f"T2med-AMDB: PZN {normalized} nicht gefunden")
            return None
        if len(rows) != 1:
            raise T2medAmdbError(f"T2med-AMDB: PZN {normalized} ist nicht eindeutig")

        row = self._json_row(rows[0])
        name = str(row.get("name") or "").strip() or None
        substance = str(row.get("substance") or "").strip() or None
        strength = str(row.get("strength") or "").strip() or None
        medication_form = str(row.get("formMedicationPlan") or "").strip() or None
        ifa_form = str(row.get("formIfa") or "").strip() or None
        substances = (
            [{"name": substance, "strength": strength}]
            if substance or strength
            else []
        )
        resolved = {
            "pzn": normalized,
            "name": name,
            "form_long": medication_form,
            "form_short": ifa_form,
            "substances": substances,
            "components": [],
        }
        self._cache[normalized] = resolved
        details = [name or "Name fehlt"]
        if substance:
            details.append(f"Wirkstoff {substance}")
        if strength:
            details.append(f"Stärke {strength}")
        if medication_form or ifa_form:
            details.append(f"Form {medication_form or ifa_form}")
        self._progress(f"T2med-AMDB: PZN {normalized}: " + " | ".join(details))
        return resolved

    def close(self) -> None:
        return None

    def __enter__(self) -> "T2medAmdbResolver":
        self.metadata()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PZN ausschließlich lesend in der aktiven T2med-AMDB auflösen"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--client", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--timeout", type=float, default=30.0)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("info", help="Aktives Schema und MariaDB-Version anzeigen")
    lookup_parser = subparsers.add_parser("lookup", help="Eine oder mehrere PZN auflösen")
    lookup_parser.add_argument("pzn", nargs="+")
    args = parser.parse_args(argv)
    try:
        with T2medAmdbResolver(
            config_path=args.config,
            client_path=args.client,
            socket_path=args.socket,
            timeout=args.timeout,
            progress=lambda message: print(message, file=sys.stderr),
        ) as resolver:
            if args.command == "info":
                print(json.dumps(resolver.metadata(), ensure_ascii=False, indent=2))
            else:
                result = {normalize_pzn(pzn): resolver.lookup(pzn) for pzn in args.pzn}
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (T2medAmdbError, OSError, ValueError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
