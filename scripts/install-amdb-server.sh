#!/usr/bin/env bash
# Install the read-only PZN service on a Debian/Ubuntu T2med host.
set -euo pipefail

if [[ $# != 1 || ${1:-} == --help ]]; then
  echo "Aufruf auf dem T2med-Server: sudo bash scripts/install-amdb-server.sh SERVER-LAN-IPV4"
  [[ ${1:-} == --help ]] && exit 0
  exit 2
fi
if (( EUID != 0 )); then echo 'Bitte auf dem T2med-Server mit sudo ausführen.' >&2; exit 1; fi
addr=$(python3 -c 'import ipaddress, sys
addr = ipaddress.IPv4Address(sys.argv[1])
if addr.is_unspecified or addr.is_multicast or addr.is_loopback or addr.is_reserved:
    sys.exit("Eine konkrete LAN-Adresse des Servers angeben.")
print(addr)' "$1")
here=$(cd "$(dirname "$0")" && pwd)
conf=/etc/kienzleocr-amdb
identity=$conf/identity
lib=/usr/local/lib/kienzleocr-amdb
account=kienzleocr-amdb
if [[ ! -S /var/opt/t2med/data/mariadb/t2med-mariadb || ! -r /opt/t2med/server/mmi/service.conf ]]; then
  echo 'T2med-Socket oder MMI-Konfiguration fehlt. Auf dem T2med-Server ausführen.' >&2
  exit 1
fi
apt-get update
apt-get install -y gunicorn openssl
if ! getent passwd "$account" >/dev/null; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$account"
fi
install -d -m 0755 "$lib" "$lib/kienzledoku_ocr_backfill"
for module in __init__ progress bfarm_pzn t2med_amdb amdb_service; do
  install -m 0644 "$here/../src/kienzledoku_ocr_backfill/$module.py" "$lib/kienzledoku_ocr_backfill/"
done
install -d -o root -g "$account" -m 0750 "$conf"
if [[ ! -d $identity ]]; then
  umask 077
  stage=$(mktemp -d "$conf/.identity.XXXXXXXX")
  trap 'rm -f "$stage"/{server.key,server.crt,token,arbeitsplatz.json,listen.env}; rmdir "$stage" 2>/dev/null || true' EXIT
  openssl req -x509 -newkey rsa:3072 -sha256 -noenc -days 730 \
    -subj "/CN=kienzleOCR PZN-Dienst" -addext "subjectAltName=IP:$addr" \
    -addext 'basicConstraints=critical,CA:FALSE' -addext 'extendedKeyUsage=serverAuth' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
    -keyout "$stage/server.key" -out "$stage/server.crt"
  python3 - "$stage" "$addr" <<'PY'
import json, secrets, sys
from pathlib import Path
directory = Path(sys.argv[1])
address = sys.argv[2] + ":18443"
token = secrets.token_urlsafe(32)
(directory / "token").write_text(token + "\n")
(directory / "listen.env").write_text("AMDB_LISTEN=" + address + "\n")
profile = {"url": "https://" + address, "ca_pem": (directory / "server.crt").read_text(), "token": token}
(directory / "arbeitsplatz.json").write_text(json.dumps(profile, indent=2) + "\n")
PY
  chgrp "$account" "$stage" "$stage"/{server.key,server.crt,token,listen.env}
  chmod 0750 "$stage"
  chmod 0640 "$stage"/{server.key,server.crt,token,listen.env}
  mv -T "$stage" "$identity"
  trap - EXIT
fi
if ! grep -Fxq "AMDB_LISTEN=$addr:18443" "$identity/listen.env"; then
  echo "Das vorhandene Profil verwendet eine andere Server-IP. Anleitung zum Profilwechsel beachten." >&2
  exit 1
fi
# Verify the exact runtime identity and both lookup paths before starting the listener.
if ! (cd "$lib" && runuser -u "$account" -- /usr/bin/python3 -c '
from kienzledoku_ocr_backfill.t2med_amdb import T2medAmdbResolver
with T2medAmdbResolver(db_user="kienzleocr-amdb") as db:
    db.lookup("09322739")
    print("AMDB-Lesezugriff bestätigt:", db.schema)
'); then
  echo "Socket-Lesekonto und Dateizugriff gemäß docs/AMDB-ARBEITSPLATZ.md prüfen, dann Installer erneut ausführen." >&2
  exit 1
fi
install -m 0644 "$here/kienzleocr-amdb.service" /etc/systemd/system/kienzleocr-amdb.service
systemctl daemon-reload
systemctl enable kienzleocr-amdb.service
systemctl restart kienzleocr-amdb.service
# Exercise the listener including TLS, the credential and access to the active schema.
for attempt in {1..10}; do
  if python3 "$here/../t2med-amdb.py" --remote-profile "$identity/arbeitsplatz.json" --timeout 3 info; then
    echo "PZN-Dienst bereit. Privates Arbeitsplatzprofil: $identity/arbeitsplatz.json"
    exit 0
  fi
  sleep 1
done
echo 'Selbsttest fehlgeschlagen. Dienstprotokoll: journalctl -u kienzleocr-amdb -n 30' >&2
exit 1
