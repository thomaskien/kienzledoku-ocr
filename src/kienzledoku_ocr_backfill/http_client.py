"""Small stdlib HTTPS client for APS and CDN with Basic Auth."""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import HttpRequestError


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError(
                f"Ungültige JSON-Antwort von {self.url}", status=self.status
            ) from exc


def make_ssl_context(insecure: bool, ca_cert: Optional[Path]) -> ssl.SSLContext:
    if insecure:
        return ssl._create_unverified_context()
    if ca_cert is not None:
        return ssl.create_default_context(cafile=str(ca_cert))
    return ssl.create_default_context()


class HttpClient:
    def __init__(
        self,
        username: str,
        password: str,
        ssl_context: ssl.SSLContext,
        *,
        timeout: float,
        user_agent: str,
    ) -> None:
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        self._authorization = f"Basic {token}"
        self._ssl_context = ssl_context
        self._timeout = timeout
        self._user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        raise_for_status: bool = True,
    ) -> HttpResult:
        request_headers = {
            "Authorization": self._authorization,
            "Accept": "application/json, */*",
            "User-Agent": self._user_agent,
        }
        if headers:
            request_headers.update(headers)

        body = None
        if json_body is not None:
            body = json.dumps(
                json_body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(
                request,
                context=self._ssl_context,
                timeout=self._timeout,
            ) as response:
                result = HttpResult(
                    status=int(getattr(response, "status", 200)),
                    headers={key: value for key, value in response.headers.items()},
                    body=response.read(),
                    url=url,
                )
        except urllib.error.HTTPError as exc:
            result = HttpResult(
                status=int(exc.code),
                headers={key: value for key, value in exc.headers.items()},
                body=exc.read(),
                url=url,
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise HttpRequestError(f"Verbindungsfehler bei {url}: {reason}") from exc

        if raise_for_status and not 200 <= result.status < 300:
            raise HttpRequestError(
                f"HTTP {result.status} bei {url}", status=result.status
            )
        return result
