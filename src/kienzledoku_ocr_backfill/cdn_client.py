"""Read-only CDN delivery client."""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

from .errors import HttpRequestError, MissingCdnError
from .http_client import HttpClient


CDN_SCHEME = "cdn://"
DEFAULT_PATIENT_PREFIX = "APS/Praxis/Patient/"


def content_path_from_reference(reference: str) -> str:
    normalized = (reference or "").strip()
    if not normalized.startswith(CDN_SCHEME):
        raise HttpRequestError("CDN-Verweis beginnt nicht mit cdn://")
    content_path = normalized[len(CDN_SCHEME) :].lstrip("/")
    if not content_path:
        raise HttpRequestError("CDN-Verweis enthält keinen contentPath")
    if content_path.startswith(DEFAULT_PATIENT_PREFIX):
        return content_path
    return DEFAULT_PATIENT_PREFIX + content_path


class CdnClient:
    def __init__(self, http: HttpClient, base_url: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")

    def download(self, reference: str, target: Path) -> int:
        content_path = content_path_from_reference(reference)
        encoded = urllib.parse.quote(content_path, safe="/")
        result = self._http.request(
            "GET",
            f"{self._base_url}/delivery/{encoded}",
            headers={"Accept": "*/*"},
            raise_for_status=False,
        )
        if result.status in (404, 410):
            raise MissingCdnError(
                f"CDN-Datei fehlt (HTTP {result.status})", status=result.status
            )
        if not 200 <= result.status < 300:
            raise HttpRequestError(
                f"CDN-Download fehlgeschlagen (HTTP {result.status})",
                status=result.status,
            )

        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(result.body)
            handle.flush()
            os.fsync(handle.fileno())
        return len(result.body)
