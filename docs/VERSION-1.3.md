# Version 1.3

## Fortschrittsausgabe pro Dokument

Der serielle Verarbeitungslauf zeigt die Identität und jeden wesentlichen
Schritt unmittelbar auf der lokalen Konsole:

```text
Identifiziere Dokument
Dokumenten-ID: <objectId>
Dokumentendatum: <Datum/Zeit>
Patient: <Patientennummer> <Patientenname, falls verfügbar>
Dokumententitel:
  <erste nichtleere Zeile>
  <zweite nichtleere Zeile>
Dokument wird geladen
Dokumentenname:
  <erste nichtleere Zeile>
  <zweite nichtleere Zeile>
OCR-Status: <noch nicht erfolgt | bereits erfolgt mit Version ... am ...>
OCR läuft
Orientierungsprüfung: <n> Seite(n)
Orientierung Seite <n>: <Entscheidung>
OCR erfolgreich
OCR-Text geschrieben
Dokument fertig
Nächstes Dokument
```

Im Dry-Run lautet die wahrheitsgemäße Meldung `OCR-Text würde geschrieben
(Dry-Run)`. `OCR-Text geschrieben` erscheint ausschließlich nach einem
erfolgreichen und verifizierten APS-Update. Ist bereits OCR vorhanden und wurde
`--reprocess` nicht gesetzt, wird die Datei weder geladen noch erneut erkannt.

Der Dokumententitel sind die ersten beiden nichtleeren Zeilen des bisherigen
APS-Felds `text`, ohne einen vorhandenen verwalteten OCR-Block. Dokumentenname
ist der Dateiname aus `aps.verweiseintragdateiinfo`. Die Anzeige kürzt weder den
gespeicherten T2med-Text noch das OCR-Ergebnis. Patientenname und Vorschau werden
nur auf der Konsole ausgegeben und nicht zusätzlich in das Journal geschrieben.

## Automatische Seitenausrichtung

Vor OCR rendert `pdftoppm` jede Seite mit 150 dpi in eine temporäre Graustufen-
Arbeitsdatei. Tesseract OSD bestimmt die notwendige Drehung. Ab der einstellbaren
Konfidenzschwelle wird die OSD-Entscheidung direkt verwendet.

Bei niedriger oder nicht auswertbarer OSD-Konfidenz prüft Version 1.3 die Lagen
0°, 90°, 180° und 270° separat. Bewertet werden erkannte Wörter, mittlere
Tesseract-Konfidenz, sprachlich plausible Wörter und typische Dokumentbegriffe.
Nur ein ausreichend gutes und gegenüber der zweitbesten Lage eindeutiges
Ergebnis wird gedreht. Andernfalls bleibt die Seite bei 0° und wird als
`uncertain` protokolliert. Dadurch wird bei unklaren Seiten keine Drehung
erzwungen.

Alle ermittelten Seitendrehungen werden mit qpdf gemeinsam auf eine temporäre
PDF-Arbeitskopie angewandt. Manuelle Angaben wie `--force-rotate-page 1:+90`
überschreiben die Automatik für die genannte Seite. Weder die T2med-CDN-Datei
noch eine andere Originaldatei wird verändert oder neu hochgeladen.

Das JSONL-Journal enthält pro OCR-Ergebnis unter `ocrDiagnostics` die Seite,
Drehung, Methode (`osd`, `four_way` oder `manual`), Konfidenz, Status und – beim
Vierfachvergleich – die Vergleichswerte. So bleibt auch nach einem Lauf
nachvollziehbar, warum eine Seite gedreht oder unverändert gelassen wurde.
