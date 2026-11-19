# kienzleOCR 2.3.1 auf dem Linux-Testarbeitsplatz

## Bestehende Installation aktualisieren

Im Tauschordner `kienzle-ocr/2.3.1` als angemeldeter T2med-Desktopbenutzer ein Terminal öffnen und ausführen.

```bash
bash TEST-UPDATE.sh
```

Das Skript prüft die Paketprüfsumme, installiert nach `~/.local/opt/kienzle-ocr-2.3.1/` und stellt den vorhandenen URI-Starter auf diese Version um. Es trägt die Adresse des Test-Commanders in `~/.config/kienzleocr/config.json` unter `commander_url` ein. FHIR-Adresse, API-Key, Zertifikat, AMDB-Profil, Journal und Laufoptionen werden übernommen. Laut Übergabe steht der Starter auf Dry-Run über den vollständigen sichtbaren Bestand. Die eingerichtete Schaltfläche in T2med startet den Test wie bisher. Die erste Statuszeile zeigt Version und Betriebsart.

## Die Beispieldateien ansehen

Im selben Terminal lässt sich das dreiseitige Beispiel direkt verarbeiten. Dieser Befehl gibt den erzeugten Text auf dem Bildschirm aus und benötigt keine FHIR-Sitzung.

```bash
python3 "$HOME/.local/opt/kienzle-ocr-2.3.1/scripts/ocr-vorschau.py" ../fax-report*.pdf
```

Bei diesem PDF sind drei Data-Matrix-Codes zu erwarten. Der strukturierte Plan kommt direkt aus dem jeweiligen Code. Fehlt am Arbeitsplatz die lokale AMDB, bleiben eingebettete Angaben und PZN sichtbar. Für Arzneimittel, die der Plan nur als PZN enthält, liefert ein eingerichteter PZN-Dienst die Stammdaten. Bei einem Ausfall des ausdrücklich eingerichteten Remote-Dienstes bleibt das Dokument zur Wiederholung offen. Ein früher bereits bestätigter Eintrag wird beim normalen FHIR-Lauf übersprungen. Für den Vergleich am selben Dokument deshalb die Vorschau verwenden. [PZN-Dienst am Arbeitsplatz einrichten](AMDB-ARBEITSPLATZ.md)

Für den direkten Vergleich mit der früheren, fehlerhaften OCR-Ausgabe den Text an den konfigurierten Praxis-Commander übergeben.

```bash
python3 "$HOME/.local/opt/kienzle-ocr-2.3.1/scripts/ocr-vorschau.py" ../ocr-ausgabe.txt
```

Der Commander liest gewöhnliche OCR-Seiten im Zusammenhang und meldet die Zeilenbereiche, die ein Mensch zuverlässig lesen kann. kienzleOCR übernimmt deren Originalwortlaut. Für einen zusammenhängenden unleserlichen Abschnitt erscheint einmal `[OCR-Abschnitt unleserlich – siehe Original.]`, auch über Leerzeilen und Seitenwechsel hinweg. Eingestreute zufällige Ziffern oder Wortfragmente unterbrechen diesen Hinweis nicht. Erkennbare klinische Angaben wie Dosen mit Einheit und Negationen sind zusätzlich lokal geschützt. Bei ungültiger Antwort oder Verbindungsfehler wird für das betroffene Dokument kein FHIR-Freitext angelegt. Im Protokoll steht die Dauer je bearbeiteter Seite.

Der Client fragt das Standardmodell über `GET /v1/models` ab und sendet die Textblöcke an `POST /v1/clinical-chat/completions`. Eine optionale Commander-Anmeldung verwendet die Umgebungsvariable `KIENZLEOCR_COMMANDER_TOKEN` des gestarteten Prozesses. Für andere Installationen die eigene Commander-Adresse eintragen. Zum Abschalten im Installationsverzeichnis `python3 scripts/install-linux-handler.py --update-existing --no-commander` ausführen.

## Automatische Token-Erneuerung für lange Läufe

Für einen mehrstündigen Lauf die **OAuth-Client-ID der T2med-Drittanbieterdefinition** einmalig am Arbeitsplatz eintragen, sofern diese noch fehlt. Im folgenden Befehl den Platzhalter durch die eigene Client-ID ersetzen.

```bash
cd "$HOME/.local/opt/kienzle-ocr-2.3.1"
python3 scripts/install-linux-handler.py --update-existing --client-id 'EIGENE_OAUTH_CLIENT_ID'
```

Beim nächsten Start aus T2med fragt kienzleOCR nach dem zugehörigen **Client-Secret** und zeigt einen Link zur Geräteanmeldung. Liefert T2med einen Refresh-Token, erscheint „Geräteanmeldung mit automatischer Token-Erneuerung aktiv“. Der Client erneuert das Zugriffstoken bei Bedarf vor dem nächsten Zugriff und übernimmt auch einen ausgetauschten Refresh-Token. Im Protokoll erscheint „FHIR-Zugriffstoken erneuert“. Die Anmeldung gilt für den aktuellen Programmlauf. Bei widerrufenem oder abgelaufenem Refresh-Token ist eine neue Geräteanmeldung erforderlich. Der bestehende Start ohne Client-ID verwendet den übergebenen Deep-Link-Token bis zu dessen Ablauf.

Die Leserechte richten sich nach den Berechtigungen der Anmeldung. Vor jedem Schreiben prüft kienzleOCR die Zuordnung zu Patient, Arztrolle und Behandlungsort. Ist ein Kontext beim Lesen abgelaufen (HTTP 404/410), legt der Client einen passenden neuen an und liest ihn zur Kontrolle zurück. Das benötigt eine freigegebene Kontextanlage in T2med ab 26.10. T2med dokumentiert für Patientenkontexte bis zu vier Stunden. [T2med-Integrationsleitfaden](https://github.com/T2med/fhir-demoapplication/blob/main/Integrationsleitfaden-FHIR-API.md)

Beim Schreiblauf speichert kienzleOCR fertige Ergebnisse bis zur verifizierten Anlage geschützt neben dem Journal unter `~/.local/state/kienzleocr/fhir.jsonl.pending/`. Sie enthalten medizinischen Text und gehören zum lokalen Desktopbenutzer. Nach einer Unterbrechung denselben Starter und dasselbe Journal verwenden. Ein unklarer Schreibausgang wird lesend mit Patientenakte, Quellkennung, Textprüfsumme und Originaldatum abgeglichen. Bei weiterhin offenem Ergebnis erscheint `unconfirmed`.
