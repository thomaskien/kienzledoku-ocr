# KienzleDoku OCR-Backfill für T2med

Version 1.00 verarbeitet T2med-PDF-Dokumentverweise (`classid = 60`) seriell. Das Programm liest das Inventar ausschließlich aus PostgreSQL, lädt die unveränderte Originaldatei über das CDN, gewinnt Text über ein austauschbares OCR-Backend und ergänzt nur das APS-Feld `text` des bestehenden Dokumentverweises.

Ohne `--apply` läuft das Programm immer als Dry-Run. Direkte Schreibzugriffe auf PostgreSQL und Änderungen an CDN-Dateien sind nicht implementiert.

## Sicherheitsmodell

- PostgreSQL: ausschließlich `SELECT` beziehungsweise `COPY (SELECT ...) TO STDOUT`
- CDN: ausschließlich `GET /cdn/rest/delivery/<contentPath>`
- APS-Lesen: `POST /praxis/verweis/dokumentverweis/find`
- APS-Schreiben: `POST /praxis/verweis/dokumentverweis/update`
- Vor dem Update wird das vollständige DTO frisch gelesen und nur dessen Feld `text` geändert.
- Vor jedem APS-Update wird `oldText` als `update_prepared` in ein append-only JSONL-Journal geschrieben und per `fsync` gesichert.
- Nach jedem Update werden Text, Footer, `objectId`, `verweis`, `gueltigkeitszeitpunkt` und `fachinformationstyp` erneut geprüft.
- Ein Dokumentfehler wird journalisiert; anschließend läuft der Batch mit dem nächsten Dokument weiter.
- Bereits vorhandene Marker `kienzledoku OCR v…` verhindern Doppelanhänge.
- Schreibvorgänge laufen bewusst nicht parallel.

`classid = 59` (Bildeinträge) ist nicht freigeschaltet. Dafür muss zuerst der eigene APS-Adapter end-to-end bestätigt werden. `--limit` wird erst nach der Dateiinfo-Prüfung angewendet und zählt damit tatsächliche PDF-Kandidaten; andere `classid-60`-Dateitypen verbrauchen das Limit nicht.

## Voraussetzungen

- Python 3.9 oder neuer
- lokaler Lesezugriff über T2meds `psql`, standardmäßig `/opt/t2med/server/postgres/bin/psql`
- T2med-Benutzer für APS und CDN
- `ocrmypdf`, Tesseract mit `deu`/`eng`, `pdftotext`, Ghostscript, qpdf und unpaper

Das Standardbackend entspricht der bestätigten KienzleFax-Pipeline: OCRmyPDF/Tesseract mit `deu+eng`, OEM 1, Seitendrehung, Entzerrung, Reinigung, 300-dpi-Oversampling, PDF/A-3, Optimierung 1, 300 Sekunden Tesseract-Zeitlimit je Seite und zwei internen Jobs. Seiten ohne Textschicht werden durch Tesseract erkannt; bei Seiten mit vorhandenem PDF-Text bleibt dieser erhalten und wird nicht unnötig erneut OCR-erkannt. Anschließend liest `pdftotext` den vorhandenen und den neu erkannten Text gemeinsam und ohne Steuerzeichen für Seitenumbrüche aus dem temporären OCR-PDF. Dieses PDF wird verworfen; die T2med-CDN-Datei wird nicht verändert.

Auf Debian/Raspberry Pi OS werden dieselben Pakete wie bei KienzleFax benötigt:

```bash
sudo ./scripts/install-ocr-dependencies.sh
```

Der Installer verwendet ausschließlich `apt-get`, führt kein Distributions-Upgrade aus und prüft danach Programme, OCRmyPDF-Optionen sowie die Tesseract-Sprachen `deu`, `eng` und `osd`. Eine rein lesende Prüfung ist ebenfalls möglich:

```bash
./scripts/install-ocr-dependencies.sh --check
```

## OCR-Backend

Ohne zusätzliche Option wird das KienzleFax-OCRmyPDF-Backend verwendet. Seine Pfade und konservativen Ressourcenwerte können bei Bedarf angepasst werden:

```bash
--ocrmypdf /usr/bin/ocrmypdf \
--pdftotext /usr/bin/pdftotext \
--ocr-language deu+eng \
--ocr-jobs 2 \
--tesseract-timeout 300
```

Die OCR-Abstraktion bleibt erhalten. Ein eigener Befehl kann ausdrücklich mit `--ocr-command` eingesetzt werden. Er wird ohne Shell gestartet; `{input}` ist verpflichtend. Liefert das Programm den Text auf stdout, genügt zum Beispiel:

```bash
--ocr-command '/pfad/zum/ocr-programm --input {input}'
```

Schreibt das Programm in eine Textdatei, wird `{output}` verwendet:

```bash
--ocr-command '/pfad/zum/ocr-programm --input {input} --output {output}'
```

Die Ausgabedatei muss UTF-8 enthalten. Der optionale Platzhalter `{mime_type}` steht ebenfalls zur Verfügung. Es wird kein Text gekürzt.

Alternativ kann der vollständige eigene Befehl in `KIENZLEDOKU_OCR_COMMAND` hinterlegt werden. Ohne diese Einstellung bleibt OCRmyPDF/Tesseract der Standard.

## Start aus dem Repository

```bash
python3 ./kienzledoku-ocr.py --help
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Eine Installation ist nicht erforderlich. Optional ist eine lokale Paketinstallation möglich:

```bash
python3 -m pip install .
kienzledoku-ocr --help
```

## Zugangsdaten

Der Benutzername kann mit `--username` oder `T2MED_OCR_USERNAME` gesetzt werden. Das Passwort wird verdeckt abgefragt. Für einen beaufsichtigten nichtinteraktiven Lauf kann `T2MED_OCR_PASSWORD` verwendet werden; es sollte niemals in einer Shell-History oder einer versionierten Datei stehen.

## Empfohlene Inbetriebnahme

Zuerst ausschließlich das bestätigte Testobjekt `test5.pdf` als Dry-Run ausführen:

```bash
python3 ./kienzledoku-ocr.py \
  --dry-run \
  --object-id 003ce75054486374405bdf673254f82ffa90 \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

`--insecure` entspricht dem bestätigten lokalen Test mit selbstsigniertem T2med-Zertifikat. Sicherer ist eine Praxis-CA über `--ca-cert /pfad/ca.pem`.

Nach Kontrolle des Dry-Runs wird genau dieses Objekt bewusst geschrieben:

```bash
python3 ./kienzledoku-ocr.py \
  --apply \
  --object-id 003ce75054486374405bdf673254f82ffa90 \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Danach schrittweise erweitern:

```bash
# Kleine Teilmenge, weiterhin Dry-Run
python3 ./kienzledoku-ocr.py --dry-run --limit 10 [Verbindungs- und OCR-Optionen]

# Ein Patient
python3 ./kienzledoku-ocr.py --apply --patient 8100 [Verbindungs- und OCR-Optionen]

# Gesamtlauf mit Wiederaufnahme
python3 ./kienzledoku-ocr.py --apply --resume [Verbindungs- und OCR-Optionen]
```

## Textformat

```text
<alter Text/Titel>


<vollständiger OCR-Text>

kienzledoku OCR v1.00, 30.08.2026 14:55
```

Die Footerzeit wird immer in `Europe/Berlin` erzeugt.

## Journal und Status

Das Journal enthält Patientennummer, Dokumentmetadaten und bei schreibenden Vorgängen den vollständigen bisherigen Text. Es enthält damit medizinische Daten, wird mit Modus `0600` angelegt und gehört in ein entsprechend geschütztes Verzeichnis.

Wichtige Statuswerte sind `dry_run`, `update_prepared`, `updated`, `already_ocr`, `missing_cdn`, `unsupported_type`, `download_failed`, `ocr_failed`, `ocr_empty`, `aps_find_failed`, `aps_update_failed` und `verification_failed`.

`update_prepared` ist ein Write-ahead-Eintrag. Folgt kein `updated`, muss der konkrete Eintrag geprüft werden; ein erneuter Backfill erkennt einen eventuell dennoch geschriebenen OCR-Marker und hängt ihn nicht doppelt an.

## Konfliktsicherer Rollback

Ohne `--apply` prüft der Rollback nur die Kandidaten:

```bash
python3 ./kienzledoku-ocr.py --rollback --dry-run \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Die tatsächliche Rücksetzung erfolgt explizit:

```bash
python3 ./kienzledoku-ocr.py --rollback --apply \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Zurückgesetzt werden ausschließlich zuvor vollständig verifizierte `updated`-Einträge. Stimmen die aktuelle Revision oder der SHA-256-Hash des aktuellen T2med-Textes nicht exakt mit dem Journal überein, wird `rollback_conflict` protokolliert und nichts geschrieben. Details stehen in [docs/ROLLBACK.md](docs/ROLLBACK.md).

## Exitcodes

- `0`: Lauf beendet, keine Fehlerstatuswerte
- `1`: Start-/Konfigurationsfehler
- `2`: Batch beendet, mindestens ein Dokument hatte einen Fehler oder Rollback-Konflikt
- `130`: interaktiv abgebrochen

Weitere praktische Prüfschritte stehen in [docs/BETRIEB.md](docs/BETRIEB.md).
