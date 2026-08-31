# Changelog

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
