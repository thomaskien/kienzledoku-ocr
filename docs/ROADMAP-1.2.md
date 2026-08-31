# Roadmap Version 1.2

## Fortschrittsausgabe pro Dokument

Version 1.2 soll jeden seriellen Verarbeitungsschritt unmittelbar und
verständlich auf der lokalen Konsole ausgeben. Der geplante Ablauf lautet:

```text
Identifiziere Dokument
Dokumenten-ID: <objectId>
Dokumentendatum: <Datum/Zeit>
Patient: <Patientennummer> <Patientenname>
Dokumententitel:
  <erste Zeile>
  <zweite Zeile>
Dokument wird geladen
Dokumentenname:
  <erste Zeile>
  <zweite Zeile>
OCR-Status: <noch nicht erfolgt | erfolgt mit Version ... am ...>
OCR läuft
OCR erfolgreich
OCR-Text geschrieben
Dokument fertig
Nächstes Dokument
```

## Verbindliche Details für die Umsetzung

- Die Schritte erscheinen in dieser Reihenfolge und werden sofort ausgegeben,
  damit auch bei einer lange laufenden OCR der aktuelle Zustand sichtbar ist.
- Dokument-ID und Dokumentdatum stammen aus dem bestehenden read-only Inventar.
- Patientennummer und Patientenname werden ausschließlich lesend über die
  bestätigte T2med-Datenquelle ermittelt.
- Dokumententitel und Dokumentenname werden jeweils auf zwei logische Zeilen
  begrenzt; der eigentliche T2med-Text und OCR-Text werden dadurch nicht gekürzt.
- Vor OCR wird der vorhandene KienzleDoku-Marker ausgewertet. Bei vorhandener OCR
  werden mindestens Version und Zeitstempel gemeldet.
- Im Dry-Run lautet die wahrheitsgemäße Meldung `OCR-Text würde geschrieben
  (Dry-Run)`; `OCR-Text geschrieben` erscheint nur nach einem erfolgreichen,
  verifizierten APS-Update.
- Fehler werden beim betroffenen Schritt ausgegeben. Danach folgen
  `Dokument fertig` und – sofern vorhanden – `Nächstes Dokument`; der Batch läuft
  wie bisher seriell weiter.
- Patientennamen und Dokumentvorschauen werden nur auf der lokalen Konsole
  angezeigt und nicht zusätzlich dauerhaft in das JSONL-Journal aufgenommen.

## Vor der Implementierung zuzuordnen

Die vorhandene Version 1.1 liefert bereits Dateiname, APS-Text,
Dokumentzeitpunkt und Patientennummer. Vor der Umsetzung wird anhand der
übergebenen APS-Schnittstelleninformation eindeutig festgelegt:

- welches APS-Feld als `Dokumententitel` angezeigt wird,
- ob `Dokumentenname` den T2med-Dateinamen oder eine zweizeilige Inhaltsvorschau
  bezeichnet,
- aus welchem bestätigten read-only Feld Vor- und Nachname des Patienten kommen.
