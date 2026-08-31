# Version 1.4

## Bundesmedikationsplan statt Seiten-OCR

Vor dem OCR-Lauf wird jede PDF-Seite mit ZXing-C++ auf Barcodes geprüft. Ein
bundeseinheitlicher Medikationsplan wird an seinem Data-Matrix-Inhalt erkannt.
Der enthaltene BMP-Datensatz ist ISO-8859-1-kodiertes XML und wird unabhängig
von der optischen Seitenausrichtung gelesen.

Für eine erkannte BMP-Seite wird kein Tesseract-Text verwendet. Stattdessen
schreibt die Pipeline eine klar abgegrenzte Darstellung mit Patientenname,
Geburtsdatum, Ausstellungsdatum, ausstellender Person, Abschnittsüberschrift und
einer tabulatorbasierten Tabelle:

```text
----- BEGINN BUNDESMEDIKATIONSPLAN -----
BUNDESMEDIKATIONSPLAN für Erika Muster, Geburtsdatum: 01.01.1975
Ausstellungsdatum: 24.01.2026 13:35
Ausgestellt durch: Dr. Beispiel

Überschrift: Dauermedikation
## Nr	| Medikament	| Dosis	| Einnahme
1	| Ramipril	| 5 mg, Tablette	| 1-0-0-0 Stück
	Kommentar: PZN 01234567
----- ENDE BUNDESMEDIKATIONSPLAN -----
```

Die vier Einnahmewerte bedeuten morgens, mittags, abends und nachts. Freitext,
Wochentag, Anwendungshinweis, Behandlungsgrund und PZN folgen als Kommentar,
ohne stille Kürzung. Enthält ein Dokument weitere Seiten, durchlaufen nur diese
die unveränderte OCRmyPDF-/Tesseract-Pipeline. Die Texte werden danach in
ursprünglicher Seitenreihenfolge verbunden.

## PZN-Auflösung

Der gelieferte BfArM-Downloader ist als `bfarm-pzn.py` eingebunden. Er erzeugt
die SQLite-Datei atomar; die OCR-Pipeline öffnet sie im SQLite-Modus `mode=ro`.
Die Datenbank ist vor dem ersten BMP-Lauf aus der vom BfArM bereitgestellten
ZIP-Lieferung anzulegen:

```bash
python3 ./bfarm-pzn.py update \
  --source-zip /pfad/zur/BfArM-Lieferung.zip \
  --db /var/lib/kienzledoku-ocr/bfarm_pzn.sqlite
```

Fehlt die Datenbank bei einem Plan mit PZN, bleibt die Seite in der normalen
OCR. Damit wird kein scheinbar vollständiger Medikationsplan ohne aufgelöste
Arzneimittelnamen erzeugt. Nicht auflösbare einzelne PZN werden sichtbar als
solche gekennzeichnet und in `ocrDiagnostics.medicationPlans` protokolliert.

## Erkennung und Diagnose

PDF-Seiten werden zuerst mit 300 dpi gerendert. Eine Seite ohne erkannten Code
wird separat mit 600 dpi wiederholt. Fehler einer einzelnen Seite brechen weder
den Extraktor noch den Dokumentenbatch ab. Das geschützte Journal erhält unter
`ocrDiagnostics.medicationPlans` nur Diagnosewerte wie Seite, Position,
Code-Typ, Auflösung, BMP-Version und PZN. Der vollständige Data-Matrix-Rohinhalt
wird dort nicht gespeichert.

Der fachlich neutrale Extraktor kann unabhängig aufgerufen oder importiert
werden:

```bash
python3 ./qr-extractor.py scan.pdf
```

```python
from kienzledoku_ocr_backfill.qr_extractor import extract_qr_codes

result = extract_qr_codes("scan.pdf")
```

Unterstützt werden PDF, PNG, JPEG und mehrseitiges TIFF. Das Ergebnis enthält
pro Code unveränderte Bytes als Base64, UTF-8-Text soweit dekodierbar, Seite,
Typ und Position. Die fachliche BMP-Interpretation bleibt davon getrennt.
