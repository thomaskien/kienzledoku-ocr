# Betriebs- und Testablauf

## 1. Vorbereitungen

1. Vollständiges T2med-Backup nach dem in der Praxis etablierten Verfahren erstellen und dessen Abschluss prüfen.
2. Sicherstellen, dass kein zweiter Backfill gegen dasselbe Journal läuft. Die Anwendung setzt zusätzlich eine exklusive Lockdatei.
3. Geschütztes Journalverzeichnis anlegen, beispielsweise `/var/lib/kienzledoku-ocr`, nur für den ausführenden Benutzer zugänglich.
4. Die von KienzleFax bestätigten OCR-Komponenten prüfen: `ocrmypdf`, `tesseract`, Sprachdaten `deu`/`eng`, `pdftotext`, Ghostscript, qpdf und unpaper.
5. T2med-Zugangsdaten nicht in Skripten oder dem Git-Repository speichern.

## 2. Netzwerkfreier Test

Im Repository:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Die Tests verwenden ausschließlich Fakes und ändern weder T2med noch PostgreSQL.

Abhängigkeiten auf einem Debian-/Ubuntu-T2med-Server installieren und prüfen:

```bash
sudo ./scripts/install-ocr-dependencies.sh
```

Spätere rein lesende Prüfung:

```bash
./scripts/install-ocr-dependencies.sh --check
```

Die Sprachliste muss mindestens `deu` und `eng` enthalten. Die Produktionspipeline verwendet dieselben OCRmyPDF-Parameter wie KienzleFax: Seiten ohne Textschicht werden OCR-erkannt, vorhandener PDF-Text wird beibehalten. Danach gewinnt `pdftotext -enc UTF-8 -nopgbrk` den vorhandenen und den neu erkannten Text gemeinsam.

Der Installer aktualisiert nur die APT-Paketlisten und installiert die benötigten Pakete. Er führt bewusst kein `apt-get upgrade` aus.

### Seitlich eingescannte Tabellen

Die automatische 90°-Drehung verwendet standardmäßig die konservative
OCRmyPDF-Sicherheitsschwelle `14`. Erkennt OCRmyPDF die Orientierung eines
tabellarischen Medikationsplans damit nicht sicher genug, wird nur dieses
Dokument zunächst im Dry-Run mit `--rotate-pages-threshold 2.0` getestet. Der
niedrigere Wert darf nicht ungeprüft auf den Gesamtlauf übertragen werden, da
er bei mehrdeutigen Seiten falsche Drehungen wahrscheinlicher macht.

Bleibt das Ergebnis bei einer Seite mit gemischten Textorientierungen trotzdem
unverändert, wird die bekannte Seite ausdrücklich in der temporären
OCR-Arbeitskopie gedreht:

```bash
--force-rotate-page 1:+90
```

Die Seitennummer ist einsbasiert, `+90` bedeutet im Uhrzeigersinn. Die
CDN-Originaldatei wird dabei weder überschrieben noch neu hochgeladen.

## 3. Testobjekt `test5.pdf`

Bestätigte Daten:

- Patient: `8100`
- ObjectId: `003ce75054486374405bdf673254f82ffa90`
- Klasse: `60`
- CDN-Verweis: `cdn://APS/Praxis/Patient/bc2ae09ec853459597a28cd1105e22dd`

Reihenfolge:

1. Dry-Run nur für diese ObjectId.
2. Journal auf `dry_run`, plausible Zeichenzahl und Hash prüfen.
3. Mit `--apply` nur diese ObjectId schreiben.
4. In T2med kontrollieren, dass Titel/alter Text, OCR und Footer korrekt sichtbar sind.
5. Journal muss zunächst `update_prepared` und danach `updated` mit höherer `revisionAfter` enthalten.
6. Denselben Befehl erneut ausführen; erwartet wird `already_ocr` ohne CDN-Download oder APS-Update.

## 4. Vorhandene OCR-Blöcke neu erzeugen

Ab Version 1.1 umschließen feste BEGINN-/ENDE-Zeilen den vollständigen
KienzleDoku-OCR-Block. Mit `--reprocess` wird dieser Block entfernt und nach
erneuter OCR einmal frisch angehängt. Der ursprüngliche Titel beziehungsweise
Text bleibt erhalten.

Für v1.00-Blöcke ohne Beginnmarke muss dasselbe Journal verfügbar sein, das den
damaligen verifizierten `updated`-Eintrag enthält. Nur bei exakt passendem
`newTextSha256` wird dessen `oldText` als Basis verwendet. Fehlt dieser sichere
Nachweis, endet das Dokument mit `reprocess_conflict` und bleibt unverändert.

```bash
T2MED_OCR_PASSWORD='' python3 ./kienzledoku-ocr.py \
  --dry-run \
  --reprocess \
  --username t2user \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Erst nach Kontrolle wird `--dry-run` durch `--apply` ersetzt. Für einen
vollständigen Neu-Lauf darf `--resume` nicht gesetzt werden.

## 5. Stufenweise Freigabe

Nach erfolgreichem Einzeltest:

1. `--dry-run --limit 10`
2. `--apply --limit 10`
3. einen vollständig kontrollierbaren Patienten mit `--patient`
4. größeren Dry-Run und Statusauswertung
5. Gesamtlauf mit `--apply --resume`

Es gibt keine parallelen APS-Schreibvorgänge. Geschwindigkeit darf erst nach vollständigem fachlichem Abnahmetest optimiert werden.

## 6. Fehlerauswertung

- `missing_cdn`: Verweiseintrag bleibt unverändert; typischer Kandidat nach Migration.
- `unsupported_type`: Kein freigegebener PDF-Dokumentverweis.
- `download_failed`: CDN-Verbindung oder anderer HTTP-Fehler als 404/410.
- `ocr_failed`: OCR-Prozess konnte nicht gestartet werden, Timeout oder Fehlerstatus.
- `ocr_empty`: OCR lief, lieferte aber keinen verwertbaren Text.
- `aps_find_failed`: Aktuelles vollständiges DTO konnte nicht sicher gelesen werden.
- `aps_update_failed`: APS bestätigte das Update nicht. Wegen möglicher unklarer Netzwerkantwort T2med-Eintrag und nachfolgenden Marker prüfen.
- `verification_failed`: Update wurde angestoßen, aber die Nachprüfung war nicht vollständig erfolgreich. Diesen Fall vor Fortsetzung fachlich prüfen.
- `reprocess_conflict`: Ein alter OCR-Block konnte nicht eindeutig und hash-verifiziert vom ursprünglichen Text getrennt werden; es wurde nichts geschrieben.

Ein Exitcode `2` bedeutet, dass der Batch alle erreichbaren Dokumente durchlaufen hat, aber mindestens ein solcher Status vorliegt.

## 7. Datenschutz

Originaldateien werden nur in einem automatisch entfernten temporären Verzeichnis mit restriktiven Dateirechten abgelegt. Das JSONL-Journal enthält dagegen dauerhaft `oldText` und weitere Patientenbezüge. Es darf nicht per E-Mail versandt, in Git aufgenommen oder unverschlüsselt auf fremde Systeme kopiert werden.
