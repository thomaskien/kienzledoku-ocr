# Kontrollierter Rollback

Der Rollback verwendet ausschließlich Journalzeilen mit Status `updated`, also Einträge, deren APS-Nachprüfung vollständig erfolgreich war.

Für jedes Dokument werden vor einem Restore beide Bedingungen geprüft:

1. Die aktuelle APS-/Datenbankrevision entspricht exakt `revisionAfter` des Update-Journals.
2. SHA-256 des aktuellen Textes entspricht exakt `newTextSha256` des Update-Journals.

Ist auch nur eine Bedingung verletzt, schreibt das Programm `rollback_conflict` und verändert den Eintrag nicht. Damit werden spätere manuelle oder technische Änderungen nicht überschrieben.

Auch beim Rollback wird unmittelbar vor dem Update das vollständige aktuelle DTO gelesen. Nur `text` wird auf `oldText` zurückgesetzt. Anschließend werden Text, `objectId`, CDN-Verweis, Gültigkeitszeitpunkt und Fachinformationstyp erneut geprüft.

Empfohlene Reihenfolge:

```bash
# Nur prüfen
python3 ./kienzledoku-ocr.py --rollback --dry-run \
  --object-id OBJECTID \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure

# Genau einen konfliktfreien Eintrag zurücksetzen
python3 ./kienzledoku-ocr.py --rollback --apply \
  --object-id OBJECTID \
  --journal /var/lib/kienzledoku-ocr/backfill.jsonl \
  --insecure
```

Nach `rolled_back` gilt die ObjectId nicht mehr als durch `--resume` erledigt. Ein späterer Backfill kann sie daher bewusst erneut verarbeiten.

Ein `verification_failed`, `aps_update_failed` oder alleinstehendes `update_prepared` wird nicht automatisch zurückgerollt, weil der tatsächlich gespeicherte Zustand nicht sicher genug bestätigt ist. Solche Fälle müssen einzeln geprüft werden.
