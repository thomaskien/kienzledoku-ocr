"""Optional T2med device login for installations requiring it for context creation."""

import base64
import getpass
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .fhir_client import Client, NoRedirect, SessionError, safe_url


def device_client(base, api_key, client_id, tls, report):
    # Auth endpoints are derived from the pinned FHIR configuration, never from the URI.
    host = urlsplit(base).hostname
    authority = "[" + host + "]" if ":" in host else host
    root = f"https://{authority}:16596/oauth2/"
    secret = getpass.getpass("T2med OAuth-Client-Secret: ")
    if not secret or not client_id or ":" in client_id:
        raise SessionError("OAuth-Client-Daten fehlen.")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=tls))

    def request(path, fields):
        req = Request(root + path, data=urlencode(fields).encode(), headers={"Authorization": "Basic " + basic})
        try:
            response = opener.open(req, timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise SessionError("Ungültige OAuth-Antwort.")
            data = json.loads(raw)
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
            return Client(base, data["access_token"], api_key, tls)
        if data.get("error") == "slow_down":
            interval += 5
        elif data.get("error") != "authorization_pending":
            raise SessionError("T2med-Geräteanmeldung nicht bestätigt.")
    raise SessionError("T2med-Geräteanmeldung abgelaufen.")
