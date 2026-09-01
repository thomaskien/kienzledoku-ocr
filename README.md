# KienzleDoku OCR-Backfill für T2med

Version 1.5.3 verarbeitet T2med-PDF-Dokumentverweise (`classid = 60`) seriell. Das Programm liest das Inventar ausschließlich aus PostgreSQL, lädt die unveränderte Originaldatei über das CDN, gewinnt Text über ein austauschbares OCR-Backend und ergänzt nur das APS-Feld `text` des bestehenden Dokumentverweises. Bundesmedikationspläne werden an ihrer Data Matrix erkannt und strukturiert ausgegeben, statt die betroffene Seite zu OCR-erkennen.

Ohne `--apply` läuft das Programm immer als Dry-Run. Direkte Schreibzugriffe auf PostgreSQL und Änderungen an CDN-Dateien sind nicht implementiert.

## Sicherheitsmodell

- PostgreSQL: ausschließlich `SELECT` beziehungsweise `COPY (SELECT ...) TO STDOUT`
- CDN: ausschließlich `GET /cdn/rest/delivery/<contentPath>`
- Kurze T2med-Verweise wie `cdn://<ID>` werden wie im bestätigten E‑Akte-Exporter zu `APS/Praxis/Patient/<ID>` normalisiert; vollständige Verweise bleiben unverändert.
- APS-Lesen: `POST /praxis/verweis/dokumentverweis/find`
- APS-Schreiben: `POST /praxis/verweis/dokumentverweis/update`
- Vor dem Update wird das vollständige DTO frisch gelesen und nur dessen Feld `text` geändert.
- Vor jedem APS-Update wird `oldText` als `update_prepared` in ein append-only JSONL-Journal geschrieben und per `fsync` gesichert.
- Nach jedem Update werden Text, Footer, `objectId`, `verweis`, `gueltigkeitszeitpunkt` und `fachinformationstyp` erneut geprüft.
- Ein Dokumentfehler wird journalisiert; anschließend läuft der Batch mit dem nächsten Dokument weiter.
- Bereits vorhandene Marker `kienzledoku OCR v…` verhindern standardmäßig Doppelanhänge.
- Version 1.1 grenzt jeden neuen OCR-Anteil mit festen BEGINN-/ENDE-Markern ab. `--reprocess` ersetzt den Block gezielt, statt ihn erneut anzuhängen.
- Seit Version 1.3 wird die Orientierung jeder PDF-Seite vor OCR geprüft; nur eine temporäre Arbeitskopie wird gedreht und die Entscheidung journalisiert.
- Version 1.5 liest BMP-Data-Matrix und PZN-Daten ausschließlich lokal aus der aktiven T2med-AMDB; die Data-Matrix-Rohdaten werden nicht in das Journal geschrieben.
- Schreibvorgänge laufen bewusst nicht parallel.

`classid = 59` (Bildeinträge) ist nicht freigeschaltet. Dafür muss zuerst der eigene APS-Adapter end-to-end bestätigt werden. `--limit` wird erst nach der Dateiinfo-Prüfung angewendet und zählt damit tatsächliche PDF-Kandidaten; andere `classid-60`-Dateitypen verbrauchen das Limit nicht.

## Voraussetzungen

- Python 3.10 oder neuer
- lokaler Lesezugriff über T2meds `psql`, standardmäßig `/opt/t2med/server/postgres/bin/psql`
- T2med-Benutzer für APS und CDN
- `ocrmypdf`, Tesseract mit `deu`/`eng`/`osd`, `pdftotext`, `pdftoppm`, Ghostscript, qpdf und unpaper
- Pillow und ZXing-C++ (`python3-pil`, `python3-zxing-cpp`) für Data Matrix
- lokaler, ausschließlich lesender Zugriff auf T2meds MariaDB-AMDB über den
  mitgelieferten Client und Socket

Das Standardbackend entspricht der bestätigten KienzleFax-Pipeline: OCRmyPDF/Tesseract mit `deu+eng`, OEM 1, Seitendrehung, Entzerrung, Reinigung, 300-dpi-Oversampling, PDF/A-3, Optimierung 1, 300 Sekunden Tesseract-Zeitlimit je Seite und zwei internen Jobs. Davor prüft Version 1.3 jede Seite mit Tesseract OSD. Ist die Lage nicht eindeutig, werden 0°, 90°, 180° und 270° anhand eines kurzen OCR-Laufs verglichen. Nur eine ausreichend deutliche Entscheidung wird auf die temporäre Arbeitskopie angewandt. Seiten ohne Textschicht werden durch Tesseract erkannt; bei Seiten mit vorhandenem PDF-Text bleibt dieser erhalten und wird nicht unnötig erneut OCR-erkannt. Anschließend liest `pdftotext` den vorhandenen und den neu erkannten Text gemeinsam und ohne Steuerzeichen für Seitenumbrüche aus dem temporären OCR-PDF. Diese Arbeitsdateien werden verworfen; die T2med-CDN-Datei wird nicht verändert.

Auf Debian/Raspberry Pi OS werden dieselben Pakete wie bei KienzleFax benötigt:

```bash
sudo ./scripts/install-ocr-dependencies.sh
```

Der Installer verwendet ausschließlich `apt-get`, führt kein Distributions-Upgrade aus und prüft danach Programme, OCRmyPDF-Optionen, Pillow/ZXing-C++, die Tesseract-Sprachen `deu`, `eng` und `osd` sowie den vorhandenen lokalen T2med-AMDB-Zugang. Eine rein lesende Prüfung ist ebenfalls möglich:

```bash
./scripts/install-ocr-dependencies.sh --check
```

## Bundesmedikationsplan und lokale T2med-AMDB

Der bundeseinheitliche Medikationsplan enthält eine Data Matrix. Version 1.4
liest deren BMP-XML ohne OCR und ersetzt nur diese PDF-Seite durch einen klar
markierten, menschenlesbaren Text. Andere Seiten desselben Dokuments durchlaufen
unverändert die normale OCR. Arzneimittelnamen, Wirkstoffe und Stärken werden
anhand der PZN direkt aus `MEDPLAN_PACKAGE` des aktiven lokalen T2med-AMDB-Schemas
ergänzt. Version 1.5 liest den Schemanamen bei jedem Programmstart aus
`/opt/t2med/server/mmi/service.conf`; ein Wechsel zwischen `mmidata1` und
`mmidata2` wird dadurch automatisch berücksichtigt.

Vor dem ersten OCR-Lauf lässt sich der ausschließlich lesende Zugriff prüfen:

```bash
python3 ./t2med-amdb.py info
```

Die beiden bestätigten Test-PZN lassen sich gemeinsam kontrollieren:

```bash
python3 ./t2med-amdb.py lookup 09322739 09531845
```

Während der Dokumentverarbeitung ist die Abfrage ausdrücklich sichtbar, zum
Beispiel:

```text
T2med-Arzneimitteldatenbank wird abgefragt: Schema mmidata1
T2med-Arzneimitteldatenbank verbunden: Schema mmidata1, MariaDB 11.4.5-MariaDB
T2med-AMDB: PZN 09322739 wird abgefragt
T2med-AMDB: PZN 09322739: Tamsulosin - 1 A Pharma 0,4 mg Retardtabletten | Wirkstoff Tamsulosin | Stärke 0,4 mg | Form RetTabl
T2med-AMDB: PZN 09531845 wird abgefragt
T2med-AMDB: PZN 09531845: Candesartan AAA 32 mg Tabletten | Wirkstoff Candesartancilexetil | Stärke 32 mg | Form Tabl
```

Jede SQL-Ausführung wird als `START TRANSACTION READ ONLY` gekapselt; der
Resolver erzeugt ausschließlich `SELECT` und greift nie direkt auf `.ibd`- oder
`.frm`-Dateien zu. Die Pfade können nötigenfalls mit `--amdb-config`,
`--amdb-client` und `--amdb-socket` angepasst werden. Ist die T2med-AMDB nicht
erreichbar oder schlägt eine PZN-Abfrage fehl, meldet das Programm dies und lässt
die betroffene Seite sicher in der normalen OCR. Die Data-Matrix-Erkennung
arbeitet standardmäßig mit 300 dpi und versucht eine nicht erkannte Seite erneut
mit 600 dpi (`--barcode-dpi` und `--barcode-retry-dpi`).
`--no-medication-plan-codes` deaktiviert diese Funktion.

Der unabhängige, fachlich neutrale Extraktor unterstützt PDF, PNG, JPEG und
mehrseitiges TIFF. Er gibt Rohdaten, UTF-8-Text soweit möglich, Base64, Seite und
Position als JSON aus:

```bash
python3 ./qr-extractor.py scan.pdf
```

## OCR-Backend

Ohne zusätzliche Option wird das KienzleFax-OCRmyPDF-Backend verwendet. Seine Pfade und konservativen Ressourcenwerte können bei Bedarf angepasst werden:

```bash
--ocrmypdf /usr/bin/ocrmypdf \
--pdftotext /usr/bin/pdftotext \
--pdftoppm /usr/bin/pdftoppm \
--qpdf /usr/bin/qpdf \
--tesseract /usr/bin/tesseract \
--ocr-language deu+eng \
--ocr-jobs 2 \
--orientation-confidence 5 \
--rotate-pages-threshold 14 \
--tesseract-timeout 300
```

`--orientation-confidence` steuert, ab welcher OSD-Konfidenz eine Lage direkt
übernommen wird. Unterhalb des Standards `5` startet automatisch der
Vierfachvergleich. `--rotate-pages-threshold` bleibt als nachgelagerte
OCRmyPDF-Sicherung erhalten. Für Diagnosezwecke kann die neue Vorprüfung mit
`--no-auto-orient-pages` deaktiviert werden.

Bei einem seitlich eingescannten, tabellarischen Dokument genügt seit Version
1.3 normalerweise ein gezielter Neu-Lauf ohne manuelle Drehangabe:

```bash
T2MED_OCR_PASSWORD='' python3 ./kienzledoku-ocr.py \
  --dry-run \
  --reprocess \
  --username t2user \
  --object-id OBJECTID_DES_DOKUMENTS \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Die Konsole meldet für jede Seite Lage, Methode und Konfidenz. Bleibt eine Seite
als `unsicher` bei 0°, lässt sich die fachlich bekannte Lage weiterhin
ausschließlich in der temporären OCR-Arbeitskopie vorgeben:

```bash
T2MED_OCR_PASSWORD='' python3 ./kienzledoku-ocr.py \
  --dry-run \
  --reprocess \
  --username t2user \
  --object-id 003ce75054486374405bdf673254f82ffa90 \
  --force-rotate-page 1:+90 \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

`--force-rotate-page` kann für mehrere Seiten wiederholt werden. Zulässige
Winkel sind `+90`, `-90`, `+180`, `-180`, `+270` und `-270`. qpdf verändert nur
eine automatisch gelöschte temporäre Kopie; CDN-Original und T2med-PDF bleiben
unverändert. Die manuelle Angabe hat für diese Seite Vorrang vor der Automatik.

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

## Erneute OCR mit einer neuen Version

`--reprocess` führt OCR bewusst auch bei bereits markierten Dokumenten erneut
aus. Ein vollständig markierter KienzleDoku-OCR-Block wird entfernt und durch
genau einen frischen Block ersetzt. Bei älteren v1.00-Einträgen ohne Beginnmarke wird
der ursprüngliche Text nur dann aus dem verwendeten Journal übernommen, wenn
dessen gespeicherter SHA-256-Hash exakt dem aktuellen T2med-Text entspricht.
Andernfalls meldet das Programm `reprocess_conflict` und schreibt nichts.

Zuerst als Dry-Run über die gewünschte Menge:

```bash
T2MED_OCR_PASSWORD='' python3 ./kienzledoku-ocr.py \
  --dry-run \
  --reprocess \
  --username t2user \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Nach fachlicher Kontrolle kann derselbe Lauf mit `--apply` wiederholt werden.
`--reprocess` und `--resume` sind absichtlich nicht kombinierbar, weil bei der
Neuverarbeitung kein bereits erfolgreiches Dokument übersprungen werden soll.

## Textformat

```text
<alter Text/Titel>


----- BEGINN kienzledoku OCR -----
<vollständiger OCR-Text>

kienzledoku OCR v1.5.3, 01.09.2026 10:00
----- ENDE kienzledoku OCR -----
```

Eine erkannte BMP-Seite erscheint innerhalb dieses Blocks beispielsweise so:

```text
----- BEGINN BUNDESMEDIKATIONSPLAN -----
BUNDESMEDIKATIONSPLAN für Erika Muster, Geburtsdatum: 01.01.1975
Ausstellungsdatum: 24.01.2026 13:35
Ausgestellt durch: Dr. Beispiel

Überschrift: Dauermedikation

Ramipril (Ramipril)
5 mg, Tablette // PZN 01234567
Einnahme: 1-0-0-0 Stück
Kommentar: -

----- ENDE BUNDESMEDIKATIONSPLAN -----
```

Die Footerzeit wird immer in `Europe/Berlin` erzeugt.

## Zeitstempel und Laufzeitanalyse

Version 1.5.3 versieht jede nichtleere Fortschrittsmeldung mit einem lokalen
Zeitstempel. Die Laufzeiten werden mit einer monotonen Uhr gemessen und deshalb
nicht durch eine nachträgliche Systemzeitkorrektur verfälscht. Ein Ausschnitt:

```text
[01.09.2026 10:11:12] Dokument wird geladen
[01.09.2026 10:11:13] Dauer CDN-Download: 0.842 s
[01.09.2026 10:11:13] OCR läuft
[01.09.2026 10:11:25] Dauer Orientierungsprüfung: 12.137 s
[01.09.2026 10:11:29] Dauer Data-Matrix/BMP-Prüfung: 3.804 s
[01.09.2026 10:12:41] Dauer OCRmyPDF: 72.115 s
[01.09.2026 10:12:42] Dauer Textextraktion/Zusammenführung: 0.391 s
[01.09.2026 10:12:42] Dauer OCR gesamt: 89.006 s
[01.09.2026 10:12:43] Gesamtzeit Dokument: 90.224 s
```

Zusätzlich stehen die Handler-Schrittzeiten im Journalfeld `timingsSeconds`.
Die OCR-internen Zeiten stehen unter
`ocrDiagnostics.timingsSeconds`. Alle Werte sind Sekunden mit
Millisekundenauflösung.

## Journal und Status

Das Journal enthält Patientennummer, Dokumentmetadaten, die seitenweisen Orientierungsentscheidungen und bei schreibenden Vorgängen den vollständigen bisherigen Text. Es enthält damit medizinische Daten, wird mit Modus `0600` angelegt und gehört in ein entsprechend geschütztes Verzeichnis. Ein ermittelter Patientenname wird nur auf der lokalen Konsole gezeigt und nicht zusätzlich journalisiert.

Wichtige Statuswerte sind `dry_run`, `update_prepared`, `updated`, `already_ocr`, `reprocess_conflict`, `missing_cdn`, `unsupported_type`, `download_failed`, `ocr_failed`, `ocr_empty`, `aps_find_failed`, `aps_update_failed` und `verification_failed`.

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

Die lokale T2med-AMDB-Anbindung beschreibt
[docs/VERSION-1.5.md](docs/VERSION-1.5.md). Die Neuerungen für Data Matrix und
Medikationspläne beschreibt [docs/VERSION-1.4.md](docs/VERSION-1.4.md). Die Fortschrittsausgabe und
Orientierungsentscheidung steht in [docs/VERSION-1.3.md](docs/VERSION-1.3.md).
Weitere praktische Prüfschritte
stehen in [docs/BETRIEB.md](docs/BETRIEB.md).
