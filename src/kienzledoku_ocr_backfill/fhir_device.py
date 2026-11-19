"""Optional T2med device login for installations requiring it for context creation."""

import base64
import getpass
import http.client
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .fhir_client import Client, NoRedirect, SessionError, safe_url
from .fhir_tls import connection_error, https_handler


def device_client(base, api_key, client_id, tls, report):
    # Auth endpoints are derived from the pinned FHIR configuration, never from the URI.
    host = urlsplit(base).hostname
    authority = "[" + host + "]" if ":" in host else host
    root = f"https://{authority}:16596/oauth2/"
    secret = getpass.getpass("T2med OAuth-Client-Secret: ")
    if not secret or not client_id or ":" in client_id:
        raise SessionError("OAuth-Client-Daten fehlen.")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    opener = build_opener(ProxyHandler({}), NoRedirect(), https_handler(tls))

    def request(path, fields):
        req = Request(root + path, data=urlencode(fields).encode(), headers={"Authorization": "Basic " + basic})
        try:
            response = opener.open(req, timeout=30)
        except HTTPError as exc:
            response = exc
        except (OSError, http.client.HTTPException) as exc:
            raise SessionError(connection_error(exc)) from None
        with response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise SessionError("Ungültige OAuth-Antwort.")
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise SessionError("Ungültige OAuth-Antwort.") from None
            if response.status != 200 and data.get("error") not in {"authorization_pending", "slow_down"}:
                raise SessionError("T2med-Geräteanmeldung abgelehnt.")
            return data

    grant = request("device_authorization", {"client_id": client_id, "scope": "t2med/aps/fhir"})
    uri = grant.get("verification_uri_complete") or grant["verification_uri"]
    if safe_url(uri).hostname != host or not str(grant["user_code"]).isprintable():
        raise SessionError("Ungültiges Ziel für die Geräteanmeldung.")
    report(f"T2med-Geräteanmeldung im Browser öffnen: {uri} (Code {grant['user_code']})")
    end = time.monotonic() + min(1800, int(grant["expires_in"]))
    interval = max(1, int(grant.get("interval", 5)))
    while time.monotonic() + interval < end:
        time.sleep(interval)
        data = request("token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                 "device_code": grant["device_code"], "client_id": client_id})
        if data.get("access_token"):
            client = Client(base, data["access_token"], api_key, tls)
            refresh_token = data.get("refresh_token")
            if refresh_token:
                def renew():
                    nonlocal refresh_token
                    grant = request("token", {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                              "client_id": client_id})
                    access = grant.get("access_token")
                    rotated = grant.get("refresh_token", refresh_token)
                    if not all(isinstance(t, str) and t and t.isascii() and t.isprintable()
                               for t in (access, rotated)):
                        raise SessionError("Token-Erneuerung abgelehnt. Geräteanmeldung wiederholen.")
                    refresh_token = rotated
                    report("FHIR-Zugriffstoken erneuert")
                    return access, grant.get("expires_in")
                client.refresh = renew
                client.set_token_lifetime(data.get("expires_in"))
                report("Geräteanmeldung mit automatischer Token-Erneuerung aktiv.")
            else:
                report("T2med hat keinen Refresh-Token geliefert. Bei Ablauf erneut aus T2med starten.")
            return client
        if data.get("error") == "slow_down":
            interval += 5
        elif data.get("error") != "authorization_pending":
            raise SessionError("T2med-Geräteanmeldung nicht bestätigt.")
    raise SessionError("T2med-Geräteanmeldung abgelaufen.")
