# Version 1.5: lokale T2med-Arzneimitteldatenbank

Version 1.5 löst die PZN eines erkannten Bundesmedikationsplans direkt gegen die
bereits auf dem T2med-Server vorhandene Arzneimitteldatenbank auf. Ein externer
Download oder eine separat zu aktualisierende PZN-Datei ist für die
OCR-Pipeline nicht mehr erforderlich.

## Datenquelle

Der aktive Schemaname wird aus folgender T2med-Konfiguration gelesen:

```text
/opt/t2med/server/mmi/service.conf
```

Maßgeblich ist `dball.dbschema`. Damit folgt das Programm einem Wechsel zwischen
`mmidata1` und `mmidata2` automatisch. Die Daten werden aus
`MEDPLAN_PACKAGE` gelesen:

- `PZN`
- `PACKAGENAMEIFA`
- `MOLECULENAME`
- `MOLECULEMASSES`
- `PHARMFORMIFACODE`
- `MEDPLANPHARMFORMCODE`

## Nur lesender Zugriff

Der Zugriff erfolgt über
`/opt/t2med/server/mariadb/bin/mariadb` und den lokalen Socket
`/var/opt/t2med/data/mariadb/t2med-mariadb`. Das Programm erzeugt ausschließlich
`SELECT`-Anweisungen, prüft den Schemanamen gegen eine strenge Zeichenliste und
kapselt jede Ausführung zusätzlich so:

```sql
START TRANSACTION READ ONLY;
SELECT ...;
ROLLBACK;
```

Die MariaDB-Dateien unter `/var/opt/t2med/data/mariadb` werden weder geöffnet,
kopiert noch verändert.

## Prüfung

```bash
python3 ./t2med-amdb.py info
python3 ./t2med-amdb.py lookup 09322739 09531845
```

Im OCR-Lauf erscheinen Verbindungsstatus und eine Zeile pro PZN. Bereits im
selben Dokument aufgelöste PZN werden aus einem Arbeitsspeicher-Cache wieder
verwendet und ebenfalls sichtbar gemeldet.

Kann die AMDB nicht gelesen werden oder scheitert eine einzelne PZN-Abfrage,
wird die Seite nicht durch eine möglicherweise unvollständige strukturierte
Ausgabe ersetzt. Sie durchläuft stattdessen die normale OCR; weitere Dokumente
werden weiterhin verarbeitet.
