# kienzleOCR 2.1.0 auf dem Linux-Arbeitsplatz

Der Arbeitsplatz liest den Data-Matrix-Code des Bundesmedikationsplans. Für jede unterschiedliche PZN im Dokument fragt er den PZN-Dienst im Praxisnetz nach Handelsname, Wirkstoff, Stärke und Darreichungsform. Einnahme, Kommentare, Patient und Aussteller übernimmt er aus dem Code. Weitere Seiten desselben PDFs durchlaufen die normale Textextraktion oder OCR.

## Paket aus dem Tauschverzeichnis verwenden

Öffne auf dem jeweiligen Rechner ein Terminal im Tauschordner `kienzle-ocr/2.1.0`. Prüfe die Dateien und entpacke das Programm in ein dauerhaftes lokales Verzeichnis. Führe alle weiteren Befehle aus dem entpackten Programmordner aus. Wer den Git-Checkout verwendet, kann dort auf Version 2.1.0 aktualisieren und den Entpackschritt überspringen.

```bash
sha256sum -c SHA256SUMS &&
mkdir -p "$HOME/.local/opt" &&
tar -xzf kienzle-ocr-2.1.0-linux.tar.gz -C "$HOME/.local/opt" &&
cd "$HOME/.local/opt/kienzle-ocr-2.1.0" &&
python3 ./kienzledoku-ocr.py --version
```

## 1. Einmalig auf dem T2med-Server

Für die Ersteinrichtung benötigt die Administration Zugriff auf den lokalen MariaDB-Socket. Der folgende Block richtet ein eigenes Lesekonto für die beiden AMDB-Wechselschemata ein. Falls das Konto bereits existiert, vor weiteren Schritten dessen Authentifizierung und Grants prüfen. Bei einer erneuten Installation mit demselben eingerichteten Konto genügt der Installer.

```bash
sudo /opt/t2med/server/mariadb/bin/mariadb --no-defaults \
  --protocol=SOCKET --user=root \
  --socket=/var/opt/t2med/data/mariadb/t2med-mariadb <<'SQL'
CREATE USER 'kienzleocr-amdb'@'localhost' IDENTIFIED VIA unix_socket;
GRANT SELECT ON mmidata1.MEDPLAN_PACKAGE TO 'kienzleocr-amdb'@'localhost';
GRANT SELECT ON mmidata2.MEDPLAN_PACKAGE TO 'kienzleocr-amdb'@'localhost';
SHOW GRANTS FOR 'kienzleocr-amdb'@'localhost';
SQL
```

Installiere den Dienst unter Ubuntu 24.04. Setze als Argument die feste LAN-IP des T2med-Servers ein. Die LAN-IP steht bei einer bestehenden Installation auch in der FHIR-Basisadresse. Der Arbeitsplatz braucht eine Verbindung zu dieser IP auf TCP-Port 18443. Eine etwaige Firewallfreigabe auf die vorgesehenen Praxisarbeitsplätze begrenzen.

```bash
read -rp 'LAN-IP des T2med-Servers: ' SERVER_IP
sudo bash scripts/install-amdb-server.sh "$SERVER_IP"
```

Der Installer legt den gleichnamigen Linux-Systembenutzer an, installiert Gunicorn und OpenSSL, erzeugt ein Serverzertifikat und einen eigenen Dienstschlüssel und prüft den Zugriff unter dem Dienstkonto. Anschließend startet er `kienzleocr-amdb.service` und fragt das aktive Schema über HTTPS ab. Die Anmeldung bei MariaDB erfolgt über die Unix-Identität des Dienstes. Pro Anfrage liest er `dball.dbschema` aus `/opt/t2med/server/mmi/service.conf`. Nach einem Wechsel zwischen `mmidata1` und `mmidata2` wird das neue Schema bei der nächsten Anfrage verwendet. [MariaDB-Dokumentation zur Socket-Anmeldung](https://mariadb.com/docs/server/reference/plugins/authentication-plugins/authentication-plugin-unix-socket)

Der Installer meldet den Pfad zum privaten **Arbeitsplatzprofil**. Übertrage genau diese Datei über einen geschützten administrativen Übertragungsweg in das Benutzerkonto des vorgesehenen Arbeitsplatzes. Das Profil enthält die Serveradresse, das öffentliche TLS-Zertifikat und den PZN-Zugangsschlüssel. Der private TLS-Schlüssel bleibt auf dem Server. Das Profil gehört weder ins allgemeine Tauschverzeichnis noch ins Git-Repository.

```text
/etc/kienzleocr-amdb/identity/arbeitsplatz.json
```

## 2. Auf dem Linux-Arbeitsplatz testen

Die Beispiele erwarten das privat übertragene Profil unter `$HOME/arbeitsplatz.json`. Als angemeldeter Desktopbenutzer ausführen. Der erste Test braucht weder einen laufenden T2med-Client noch ein Patientendokument.

```bash
chmod 600 "$HOME/arbeitsplatz.json"
python3 ./t2med-amdb.py --remote-profile "$HOME/arbeitsplatz.json" info &&
python3 ./t2med-amdb.py --remote-profile "$HOME/arbeitsplatz.json" lookup 09322739 09531845
```

Erwartet werden der Name des aktiven Schemas und je eine Antwort pro PZN. Im bekannten Testbestand entspricht `09322739` Tamsulosin – 1 A Pharma 0,4 mg Retardtabletten und `09531845` Candesartan AAA 32 mg Tabletten.

## 3. FHIR-Handler für strukturierte Pläne einrichten

Bei bestehender FHIR-Einrichtung genügt der Profilimport aus dem **neuen Programmordner**. Der Befehl registriert den Handler als Dry-Run. Für den Plan-Test lässt er sich mit `--patient FHIR-ID` auf einen Testpatienten einschränken.

```bash
python3 scripts/install-linux-handler.py --amdb-profile "$HOME/arbeitsplatz.json"
```

Für einen **neuen Arbeitsplatz** zuerst die OCR-Pakete installieren. Danach den Handler mit der vollständigen FHIR-Basisadresse aus T2med und dem Profil registrieren. Verwende die FHIR-Basis deiner Installation. Bei einer eigenen Praxis-CA die Option `--ca-file /pfad/zur/praxis-ca.pem` ergänzen. Den T2med-Produkt-API-Key am verdeckten Prompt eingeben.

```bash
sudo bash scripts/install-ocr-dependencies.sh --desktop
read -rp 'Vollständige FHIR-Basis-URL aus T2med: ' FHIR_BASE
python3 scripts/install-linux-handler.py \
  --base-url "$FHIR_BASE" \
  --amdb-profile "$HOME/arbeitsplatz.json"
xdg-mime query default x-scheme-handler/kienzleocr
```

Erwartete Registrierung ist `kienzleocr.desktop`. In T2med den vorhandenen kienzleOCR-Knopf verwenden. Für einen neuen Knopf die folgende Aufruf-URL hinterlegen.

```text
kienzleocr://start?kontextId=${kontextId}&fhirBasisUrl=${fhirBasisUrl}&oAuthToken=${oAuthToken}
```

Für den Test ein PDF mit lesbarem BMP-Code **ohne bisherigen OCR-Eintrag** verwenden. Im Terminal erscheinen die HTTPS-Verbindung, das Schema, jede angefragte PZN mit Ergebnis und die ersetzte Seite. Bereits verarbeitete Dokumente werden übersprungen. Im Dry-Run erfolgt die gesamte Erkennung. Die tatsächliche Anlage eines neuen Freitexts mit dem Originaldatum erfordert eine eigene Registrierung mit `--apply`. Laufbegrenzungen bei jeder Registrierung erneut angeben. `--limit 1` zählt den ersten PDF-Kandidaten, auch wenn für ihn bereits OCR vorhanden ist.

Der Installer speichert das AMDB-Profil in `~/.config/kienzleocr/config.json` unter `amdb_remote` (bei gesetztem `XDG_CONFIG_HOME` entsprechend dort). Spätere Registrierungen für dieselbe FHIR-Basis behalten es bei. Mit `python3 scripts/install-linux-handler.py --local-amdb` schaltest du auf den lokalen AMDB-Socket zurück. Beim Wechsel der FHIR-Basis das dazugehörige Profil neu importieren.

## Welche Daten über die Verbindung gehen

| Vorgang | Weg | Übertragene Fachdaten |
| --- | --- | --- |
| Schema prüfen | Arbeitsplatz → `GET /v1/info` per HTTPS an den PZN-Dienst | Antwort mit aktivem Schema, Quelltabelle und MariaDB-Version |
| Medikament auflösen | Arbeitsplatz → `POST /v1/lookup` per HTTPS an den PZN-Dienst | Anfrage z. B. `{"pzn":"09322739"}`. Antwort mit PZN, Arzneimittelname, Wirkstoff, Stärke und Form oder `drug: null` |
| Arzneimitteldaten lesen | PZN-Dienst → lokaler MariaDB-Socket auf dem T2med-Server | Lesende Transaktion auf `MEDPLAN_PACKAGE` im aktiven Schema. Schemaauskunft über `information_schema.TABLES` |

Die HTTPS-Aufrufe verwenden einen eigenen Bearer-Schlüssel und prüfen den Servernamen bzw. die IP gegen das Zertifikat im Profil. PDF, Data-Matrix-Rohinhalt, Patientendaten, Einnahme und FHIR-Zugangsdaten bleiben außerhalb der PZN-Anfrage. PDF-Abruf und Anlage eines OCR-Freitexts nutzen weiterhin die bereits eingerichtete FHIR-Verbindung.

## Fehler eingrenzen

- **Keine Verbindung** – Auf dem Server `systemctl status kienzleocr-amdb` und `journalctl -u kienzleocr-amdb -n 30` prüfen. Den Einzeltest aus Abschnitt 2 am Arbeitsplatz wiederholen.
- **Zertifikatsfehler oder HTTP 401** – Das aktuelle Arbeitsplatzprofil vom richtigen Server verwenden. Die IP im Profil muss zum Zertifikat passen.
- **HTTP 503** – Die lokale Datenbankabfrage ist fehlgeschlagen. Auf dem Server den Installer mit derselben IP erneut ausführen. Sein Lesetest zeigt die Ursache unter dem Dienstkonto. Dieses benötigt Zugriff auf den Socket, auf das MariaDB-Programm und Lesezugriff auf die MMI-Konfiguration. Bei restriktiven Herstellerrechten die gezielte Berechtigung mit der Serveradministration klären. Grants für beide Wechselschemata prüfen.
- **PZN unbekannt** – Die PZN und die Angaben aus dem Plan werden ausgegeben. Bei einem Verbindungsfehler oder einer inkonsistenten Antwort bleibt das betroffene Dokument offen. Andere Dokumente laufen weiter. Nach Behebung denselben FHIR-Lauf erneut starten.
- **Profilwechsel oder Zertifikatserneuerung** – Der Installer verwendet vorhandene Schlüssel bei Updates weiter. Das erzeugte Zertifikat gilt 730 Tage. Zum geplanten Wechsel auf dem Server den Dienst stoppen, das Verzeichnis `/etc/kienzleocr-amdb/identity` unter einem anderen geschützten Namen sichern, den Installer mit der gewünschten IP ausführen und das neue Profil auf allen berechtigten Arbeitsplätzen importieren.
