"""Runtime configuration with conservative T2med defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class T2medConfig:
    server: str = "127.0.0.1"
    aps_port: int = 16567
    cdn_port: int = 16570
    timeout: float = 60.0
    insecure: bool = False
    ca_cert: Optional[Path] = None
    psql_path: Path = Path("/opt/t2med/server/postgres/bin/psql")
    db_host: str = "/tmp"
    db_port: int = 16569
    db_user: str = "t2med"
    db_name: str = "t2med"

    @property
    def aps_base_url(self) -> str:
        return f"https://{self.server_for_url}:{self.aps_port}/aps/rest"

    @property
    def cdn_base_url(self) -> str:
        return f"https://{self.server_for_url}:{self.cdn_port}/cdn/rest"

    @property
    def server_for_url(self) -> str:
        if ":" in self.server and not self.server.startswith("["):
            return f"[{self.server}]"
        return self.server
