# Changelog

## 1.5.1 – 31.08.2026

- Data-Matrix-Decoder mit Ubuntu 24.04s älterer `zxingcpp`-API kompatibel:
  automatischer zweiter Aufruf ohne den dort unbekannten Parameter `try_invert`
- Decoder-, Render- und Abhängigkeitsfehler werden im normalen OCR-Lauf sichtbar
  auf der Konsole ausgegeben statt nur in den Diagnosedaten gespeichert
- Eindeutige Abschlussmeldung mit Anzahl gefundener Codes beziehungsweise
  `kein Code gefunden` und Anzahl geprüfter Seiten

## 1.5 – 31.08.2026

- PZN-Auflösung der Bundesmedikationspläne direkt über die aktive lokale
  T2med-Arzneimitteldatenbank statt über eine separat gepflegte SQLite-Datei
- Aktives Schema wird bei jedem Start aus T2meds `mmi/service.conf` gelesen;
  `mmidata1`/`mmidata2`-Wechsel werden automatisch berücksichtigt
- Ausschließlich lesende MariaDB-Abfragen über T2meds mitgelieferten Client und
  lokalen Socket; jede Ausführung ist zusätzlich in eine
  `START TRANSACTION READ ONLY`-Transaktion eingeschlossen
- Arzneimittelauflösung aus `MEDPLAN_PACKAGE` mit PZN, Handelsname, Wirkstoff,
  Stärke und Medikationsplan-Darreichungsform
- Deutliche Konsolenausgabe beim Verbindungsaufbau und eine Ergebniszeile für
  jede abgefragte oder nicht gefundene PZN
- Eigenständige Prüf-CLI `t2med-amdb.py`; Installer prüft Konfiguration, Client,
  aktives Schema und Socket, ohne T2med-Daten zu verändern
- Sichere Rückkehr zur normalen Seiten-OCR bei Verbindungs- oder
  PZN-Abfragefehlern; der übrige Dokumentenbatch läuft weiter

## 1.4.1 – 31.08.2026

- BfArM-Bezugsweg korrigiert: Die vollständigen Referenzdaten werden nicht als
  öffentliche DSV-Links angeboten, sondern vom BfArM als ZIP bereitgestellt
- Direkter und pfadsicherer Import der offiziellen Lieferung mit
  `bfarm-pzn.py update --source-zip DATEI.zip`
- Fehlermeldung nennt den offiziellen Kontakt `Referenzdaten@bfarm.de` und
  unterscheidet die Datenlieferung eindeutig vom Resolver-Programmarchiv

## 1.4 – 31.08.2026

- Generische, importierbare QR-/Data-Matrix-Erkennung für PDF, PNG, JPEG und
  mehrseitiges TIFF mit Rohbytes/Base64, Position, Seite und isolierten Fehlern
- PDF-Erkennung zunächst mit 300 dpi und seitenweiser Wiederholung mit 600 dpi
- BMP-Data-Matrix wird nach KBV-Schema als ISO-8859-1-XML gelesen; die
  betroffene Seite wird nicht OCR-erkannt
- Menschliche BMP-Ausgabe mit klarer BEGINN-/ENDE-Markierung, Patienten- und
  Ausstellungsdaten sowie tabulatorbasierter Tabelle für T2med
- Gelieferter BfArM-§31b-Downloader/PZN-Auflöser eingebunden; die erzeugte
  SQLite-Datenbank wird von der OCR-Pipeline ausschließlich lesend geöffnet
- Unveränderte OCR aller Nicht-BMP-Seiten; gemischte Dokumente werden in ihrer
  ursprünglichen Seitenreihenfolge zusammengeführt
- Installer ergänzt `python3-pil` und `python3-zxing-cpp` und prüft die
  erforderliche OCRmyPDF-Option `--pages`
- Eigenständige CLIs `qr-extractor.py` und `bfarm-pzn.py`

## 1.3 – 31.08.2026

- Automatische seitenweise Orientierungsprüfung vor OCR: Tesseract OSD erkennt
  eindeutige 0°-/90°-/180°-/270°-Lagen; unsichere Seiten werden durch einen
  OCR-Vergleich aller vier Lagen bewertet
- Temporäre qpdf-Drehung nur der betroffenen Seiten; CDN-Originaldateien bleiben
  unverändert
- Manuelle `--force-rotate-page`-Angaben haben weiterhin Vorrang vor der Automatik
- Orientierungsentscheidung, Methode, Konfidenz und Vergleichswerte werden im
  geschützten JSONL-Journal festgehalten
- Ausführliche, schrittweise Fortschrittsausgabe pro Dokument mit Dokument-ID,
  Datum, Patientennummer und – soweit in der T2med-Tabelle vorhanden – Name
- Dokumententitel und Dokumentenname mit jeweils höchstens zwei Anzeigezeilen
- Meldung, ob und wann bereits eine KienzleDoku-OCR erfolgt ist
- Sichtbare Statusübergänge für Laden, Orientierung, OCR, APS-Schreiben,
  Abschluss und nächstes Dokument
- Zusätzliche Programmabhängigkeit `pdftoppm` wird vom Installer geprüft

## 1.1.1 – 31.08.2026

- Erzwungene, seitenbezogene +90°/-90°-Drehung einer temporären OCR-Arbeitskopie
- qpdf-Vorprüfung, ohne Änderung der CDN-Originaldatei

## 1.1 – 31.08.2026

- Klare BEGINN-/ENDE-Markierung um jeden neu erzeugten OCR-Block
- Bewusster `--reprocess`-Modus zum sicheren Ersetzen vorhandener OCR-Blöcke
- Hash-verifizierte Übernahme des ursprünglichen Textes bei v1.00-Altdaten
- Einstellbare OCRmyPDF-Sicherheitsschwelle für schwierige 90°-Seitendrehungen

## 1.00 – 31.08.2026

- Serieller OCR-Backfill für PDF-Dokumentverweise (`classid = 60`)
- Read-only PostgreSQL-Inventarisierung und Revisionsabfrage
- Bestätigte APS-find/update- und CDN-delivery-Wege
- Bestätigtes KienzleFax-Standardbackend mit OCRmyPDF/Tesseract und `pdftotext`
- Zusätzlich austauschbares, shell-freies OCR-Kommando-Backend
- Kleiner idempotenter Debian/Ubuntu-Paketinstaller mit rein lesendem `--check`-Modus
- PDF-Dateiinfo wird vor `--limit` gefiltert, damit das Limit nur geeignete PDF-Kandidaten zählt
- Kurze T2med-CDN-Verweise werden mit dem bestätigten Präfix `APS/Praxis/Patient/` normalisiert
- Default-Dry-Run, Filter, Limit und Resume
- Idempotenzmarker und Europe/Berlin-Footer
- Fsync-gesichertes JSONL-Write-ahead-Journal
- Per-Dokument-Fehlerbehandlung einschließlich fehlender CDN-Dateien
- Strikte Nachverifikation und konfliktgesicherter Rollback
- Netzwerkfreie automatisierte Tests
