# Verbindliche Projektregeln

## Schutzregeln

- PostgreSQL ausschließlich für `SELECT`/Inventarisierung verwenden. Nie `UPDATE`, `INSERT` oder `DELETE` gegen T2med-Tabellen.
- T2med-Schreibvorgänge ausschließlich über den bestätigten APS-Updateendpoint ausführen.
- CDN-Originaldateien nie verändern oder neu hochladen.
- Vor jedem Update das vollständige aktuelle DTO lesen, nur `text` ändern und das vollständige DTO zurücksenden.
- Vor dem Update `oldText` fsync-sicher journalisieren; nach dem Update erneut lesen und verifizieren.
- Default bleibt Dry-Run. Echte Änderungen ausschließlich mit `--apply`.
- APS-Schreibvorgänge bleiben seriell.
- Ein Dokumentfehler darf den Batch nicht abbrechen.
- Keine stille Textkürzung und keine Doppelanhänge.
- Rollback nur bei exakter Übereinstimmung von aktueller Revision und aktuellem Text-Hash.

## Scope Version 1.00

- Unterstützt: PDF-Dokumentverweise `classid = 60`
- Nicht unterstützt: Bildeinträge `classid = 59`; eigener end-to-end bestätigter Adapter erforderlich
- Standard-OCR ist die bestätigte KienzleFax-Pipeline mit OCRmyPDF/Tesseract (`deu+eng`) und anschließender Volltextausgabe über `pdftotext`; die Backend-Abstraktion muss erhalten bleiben.

## Prüfung

Vor Commit mindestens ausführen:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n scripts/install-ocr-dependencies.sh
git diff --check
```
