# DrivePulse mit Azure und Home Assistant

Dieser Fork ergänzt den Azure-Modus; der direkte Webhook bleibt erhalten.
MIT-Lizenz und Copyright des ursprünglichen Projekts bleiben unverändert.
Entwicklungsbranch: `feature/azure-sync`. Vorschauversion: `0.2.0b1`.
Getestet mit **Home Assistant Core 2026.10.2** (Python 3.14).

## Installation mit HACS

1. In HACS unter den benutzerdefinierten Repositories
   `https://github.com/a01wielo/drivepulse-home-assistant` als **Integration** hinzufügen.
2. Die Vorschau **v0.2.0b1** auswählen und herunterladen. Falls nötig, in HACS
   die Anzeige von Vorabversionen aktivieren.
3. Home Assistant neu starten.
4. Unter Einstellungen → Geräte & Dienste → Integration hinzufügen → DrivePulse
   den Modus **Azure synchronization** wählen.

Es darf nur ein `custom_components/drivepulse` installiert sein. Falls bereits
DrivePulse aus dem Original-Repository installiert ist, dessen HACS-Verwaltung
auf den Fork umstellen bzw. den bisherigen Download ersetzen. Den bestehenden
DrivePulse-Konfigurationseintrag in Home Assistant dabei behalten; er lässt
sich über **Konfigurieren** auf Azure umstellen. Der Moduswechsel behält die
Geräte-/Entity-IDs bei. Vor dem Wechsel ein Home-Assistant-Backup erstellen.
HACS verwaltet nicht zwei unabhängige Integrationen mit derselben Domain.

Als Alternative enthält das bereitgestellte Installations-ZIP den Ordner
`custom_components/drivepulse`. Diesen im HA-Konfigurationsverzeichnis ersetzen,
nicht einen zweiten Ordner anlegen, anschließend neu starten.

## Azure-Einstellungen

| Feld | Wert |
| --- | --- |
| API-Adresse | Die HTTPS-Adresse deiner Azure Function einschließlich `/api/telemetry` |
| Lesetoken | Nur den READ_TOKEN, ohne `Bearer` verwenden |
| Fahrzeugpartition | `default`, sofern das Backend nicht anders konfiguriert ist |
| Abfrageintervall | Standard 30 Sekunden; 10–3600 Sekunden möglich |

Die Fahrzeugpartition ist der technische API-Parameter `vehicle`, nicht der in
DrivePulse angezeigte Autoname. Die Zuordnung der Geräte erfolgt weiterhin über
das Feld `vehicle` innerhalb der DrivePulse-Nachrichten.

Die Einrichtung prüft den Zugang mit einer lesenden Anfrage. Die App behält ihre
Azure-URL und den **Schreibtoken** im Authorization-Header. Home Assistant nutzt
nur den **Lesetoken** und braucht weder Storage-Schlüssel noch eingehende
Internetverbindungen. Ein abgewiesener Token startet den Reauthentifizierungsdialog.
In **Konfigurieren** können API-Adresse, Partition, Intervall und Token geändert
werden; ein leeres optionales Token-Feld behält den vorhandenen Token.

## Was nach der Einrichtung passiert

- Die erste Synchronisierung beginnt am Anfang der Cloud-Historie. Alte
  Fahrtstart-/Fahrtende-Ereignisse können deshalb bestehende Automationen auslösen.
  Beim ersten Import solche Automationen bei Bedarf pausieren.
- Testnachrichten (`event=test`) und unbekannte Eventtypen werden wie im Original
  angenommen und ignoriert. Sie erzeugen **kein Fahrzeug und keine Sensoren**.
  Die erste echte `live`-, `trip_start`- oder `trip_end`-Nachricht erzeugt das Gerät.
- Sensorwerte und Events nutzen denselben Handler wie der direkte Webhook.
- Cursor, die letzten 1000 Event-IDs und Fahrzeugzustände werden zusammen im
  Home-Assistant-Storage gespeichert. Nach Neustart wird fortgesetzt. Im Azure-Modus
  bleiben auch die zuletzt gelesenen Live-Werte erhalten.
- Eine Seite wird erst bestätigt, wenn ihre Nachrichten erfolgreich verarbeitet
  wurden und der Zustand gespeichert wurde. Bereits verarbeitete IDs werden bei
  Wiederholung übersprungen. Ein Absturz genau zwischen einem Automationsevent
  auf dem HA-Bus und dem Speichern kann ein solches Event trotzdem wiederholen:
  Automationen sollten idempotent sein. Es wird keine Exactly-once-Garantie versprochen.
- Pro Durchlauf werden maximal zehn Seiten zu je 100 Einträgen gelesen. Bei
  weiterem Rückstand folgt ein weiterer Durchlauf nach einer Sekunde.
- Netzwerk-/Serverfehler werden mit Backoff wiederholt; HTTP 429 berücksichtigt
  einen numerischen Retry-After-Wert (bis 3600 Sekunden). Ansonsten gilt der
  reguläre Backoff. Vorhandene Sensorwerte bleiben als zuletzt bekannte Werte stehen,
  bis neue Nachrichten eintreffen; sie sind kein Nachweis einer aktuellen OBD-Verbindung.
- Wechsel von API-Adresse oder Partition startet einen neuen Cursor; bisherige
  Geräte bleiben bestehen. Änderungen des serverseitigen CURSOR_SECRET machen alte
  Cursor ungültig und erfordern einen bewusst neu eingerichteten Sync-Zustand.

## Sicherheit und Fehlersuche

Keine Tokens, Payloads oder Cursor werden von der Azure-Synchronisierung geloggt.
Diagnosen enthalten nur Modus, Intervall, Anzahl der Fahrzeuge und Sync-Metadaten.
Token-Felder sind in Formularen maskiert und werden nicht vorausgefüllt. Wie bei
anderen HA-Integrationen liegen Zugangsdaten in der lokalen Config-Entry-Verwaltung;
Backups und Zugriff auf das Konfigurationsverzeichnis entsprechend schützen.

Keine Redirects bei API-Aufrufen; nur HTTPS-Adressen ohne eingebettete Zugangsdaten
oder Query-Parameter sind zulässig. API-Antworten sind auf 4 MiB begrenzt.
Bei Ausfällen unter Einstellungen → System → Protokolle nach `drivepulse` suchen.
Dort werden Fehler ohne die geheimen Details gemeldet.

## Tests und Prüfgrenzen

`pip install -r requirements_test.txt` und `pytest -q` führen die Upstream-Tests
plus Azure-Protokoll-, Sensor-, Neustart-, Wiederholungs- und Fehlerfalltests aus.
Die Testabhängigkeit ist für Core 2026.10.2 gepinnt; GitHub Actions nutzt Python 3.14.

Die Tests prüfen echte Home-Assistant-Core-Objekte mit kontrollierten API-Antworten.
Die Einrichtung auf dem persönlichen HA-OS-System und der erste reale Sensorupdate
werden anschließend mit dem Nutzer geprüft. Es sind keine persönlichen Tokens oder
realen Fahrzeugdaten im Repository enthalten.
