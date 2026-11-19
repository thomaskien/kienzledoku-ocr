"""Client for the read-only kienzleOCR PZN service."""
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit

from .bfarm_pzn import normalize_pzn
from .progress import timed_step
from .t2med_amdb import T2medAmdbError, describe_drug


def load_amdb_profile(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise T2medAmdbError('AMDB-Profil mit chmod 600 schützen.')
        profile = json.load(stream)
    RemoteAmdbResolver(profile)  # Validate the endpoint, credential and certificate before saving.
    return profile


class RemoteAmdbResolver:
    def __init__(self, profile, *, timeout=30.0, progress=None):
        if not isinstance(profile, dict) or set(profile) != {"url", "token", "ca_pem"}:
            raise T2medAmdbError("Unvollständiges AMDB-Arbeitsplatzprofil.")
        if not all(isinstance(v, str) for v in profile.values()):
            raise T2medAmdbError("Ungültiges AMDB-Arbeitsplatzprofil.")
        url = urlsplit(profile["url"])
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.path not in ("", "/") or url.query or url.fragment
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", profile["token"])):
            raise T2medAmdbError("HTTPS-Adresse oder Zugangsschlüssel im AMDB-Profil ungültig.")
        self.host, self.port = url.hostname, url.port or 443
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            self._tls.load_verify_locations(cadata=profile["ca_pem"])
        except (ValueError, ssl.SSLError) as exc:
            raise T2medAmdbError("Zertifikat im AMDB-Profil ungültig.") from exc
        self._token = profile["token"]
        self._timeout = timeout
        self._progress = progress or (lambda message: None)
        self._cache = {}
        self._metadata = None

    def _request(self, path, body=None):
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self._timeout, context=self._tls)
        try:
            conn.request("GET" if body is None else "POST", path,
                         None if body is None else json.dumps(body).encode("ascii"), headers)
            response = conn.getresponse()
            if response.status != 200:
                raise T2medAmdbError(f"PZN-Dienst meldet HTTP {response.status}.")
            data = response.read(65537)
            if len(data) > 65536 or response.headers.get_content_type() != "application/json":
                raise T2medAmdbError("Unerwartete Antwort des PZN-Dienstes.")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ssl.SSLCertVerificationError as exc:
            raise T2medAmdbError("Zertifikatsprüfung des PZN-Dienstes fehlgeschlagen. Arbeitsplatzprofil prüfen.") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise T2medAmdbError("PZN-Dienst nicht erreichbar oder TLS-Verbindung unterbrochen.") from exc
        except (ValueError, RecursionError) as exc:
            raise T2medAmdbError("PZN-Dienst lieferte ungültiges JSON.") from exc
        finally:
            conn.close()

    def metadata(self):
        if self._metadata is None:
            self._progress(f"T2med-Arzneimitteldatenbank wird über HTTPS abgefragt ({self.host}:{self.port})")
            with timed_step("T2med-AMDB-Verbindung", self._progress):
                meta = self._request("/v1/info")
            if (meta.get("sourceTable") != "MEDPLAN_PACKAGE" or
                    not all(isinstance(meta.get(k), str) and meta[k] for k in ("schema", "serverVersion"))):
                raise T2medAmdbError("Ungültige Schemaauskunft des PZN-Dienstes.")
            self._metadata = meta
            self._progress(f"T2med-Arzneimitteldatenbank verbunden: Schema {meta['schema']}, MariaDB {meta['serverVersion']}")
        return self._metadata

    def lookup(self, pzn):
        pzn = normalize_pzn(pzn)
        if not re.fullmatch(r"[0-9]{8}", pzn):
            raise T2medAmdbError("Ungültige PZN.")
        if pzn in self._cache:
            self._progress(f"T2med-AMDB: PZN {pzn} wird erneut verwendet")
            return self._cache[pzn]
        self._progress(f"T2med-AMDB: PZN {pzn} wird abgefragt")
        with timed_step(f"T2med-AMDB PZN {pzn}", self._progress):
            reply = self._request("/v1/lookup", {"pzn": pzn})
        if set(reply) != {"drug"}:
            raise T2medAmdbError("Ungültige Arzneimittelauskunft.")
        drug = reply["drug"]
        if drug is not None:
            if (not isinstance(drug, dict) or drug.get("pzn") != pzn
                    or not _text_fields(drug, ("name", "form_long", "form_short"))
                    or not isinstance(drug.get("substances"), list)
                    or not all(_text_fields(s, ("name", "strength")) for s in drug["substances"])):
                raise T2medAmdbError(f"Ungültiger Arzneimitteldatensatz für PZN {pzn}.")
        self._progress(f"T2med-AMDB: PZN {pzn}: " + (describe_drug(drug) if drug else "nicht gefunden"))
        self._cache[pzn] = drug
        return drug

    def close(self):
        self._cache.clear()


def _text_fields(record, keys):
    return isinstance(record, dict) and all(
        k in record and (record[k] is None or isinstance(record[k], str)) for k in keys
    )
