"""WSGI endpoints for read-only PZN queries on the T2med host."""
import hmac
import json
from pathlib import Path
import re

from .t2med_amdb import T2medAmdbError, T2medAmdbResolver


def create_app(token, resolver_factory=T2medAmdbResolver):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Ungültiger PZN-Dienstschlüssel.")
    expected = ("Bearer " + token).encode("ascii")

    def application(env, start_response):
        def reply(status, data=None):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            start_response(status, [("Content-Type", "application/json"),
                                    ("Cache-Control", "no-store"), ("Content-Length", str(len(body)))])
            return [body]

        if not hmac.compare_digest(env.get("HTTP_AUTHORIZATION", "").encode("utf-8"), expected):
            return reply("401 Unauthorized")
        route = (env["REQUEST_METHOD"], env["PATH_INFO"])
        if env.get("QUERY_STRING") or route not in (("GET", "/v1/info"), ("POST", "/v1/lookup")):
            return reply("404 Not Found")
        if route[0] == "POST":
            if env.get("CONTENT_TYPE") != "application/json":
                return reply("415 Unsupported Media Type")
            try:
                length = int(env.get("CONTENT_LENGTH") or "0")
                if not 1 <= length <= 128:
                    return reply("400 Bad Request")
                obj = json.loads(env["wsgi.input"].read(length))
                if (not isinstance(obj, dict) or set(obj) != {"pzn"} or not isinstance(obj["pzn"], str)
                        or not re.fullmatch(r"[0-9]{8}", obj["pzn"])):
                    return reply("400 Bad Request")
            except (ValueError, OSError):
                return reply("400 Bad Request")
        try:
            resolver = resolver_factory()
            try:
                result = resolver.metadata() if route[0] == "GET" else {"drug": resolver.lookup(obj["pzn"])}
            finally:
                resolver.close()
        except (T2medAmdbError, OSError, ValueError):
            return reply("503 Service Unavailable")
        return reply("200 OK", result)

    return application


def from_server_config():
    token = Path("/etc/kienzleocr-amdb/identity/token").read_text(encoding="ascii").strip()
    return create_app(token, lambda: T2medAmdbResolver(db_user="kienzleocr-amdb"))
