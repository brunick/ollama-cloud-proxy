# Changelog

Alle wichtigen Änderungen an diesem Projekt werden in dieser Datei dokumentiert.

## [1.20.4] - 2026-01-18
### Added
- 🔗 **Root Redirect**: Aufrufe der Root-URL (`/`) werden nun automatisch zum Dashboard weitergeleitet (#37).
- 🚦 **System Health Indicator**: Das Dashboard zeigt nun den Echtzeit-Status des Proxys und der Ollama Cloud Verbindung an (#37).

## [1.20.3] - 2026-01-18
### Added
- 🏷️ **Version Display**: Die aktuelle Version wird nun im Dashboard angezeigt und automatisch während des CI/CD-Prozesses aus dem Release-Tag generiert (#35).

## [1.20.2] - 2026-01-16
### Optimized
- ⚡ **Health-Check Caching**: API-Key Health-Checks werden nun im Hintergrund durchgeführt und die Ergebnisse gecached. Der Dashboard-Aufruf ist dadurch nahezu verzögerungsfrei (#32).

## [1.20.1] - 2026-01-16
### Fixed
- 🚀 **Dashboard-Performance**: Parallelisierung der API-Key Health-Checks mittels `asyncio.gather` reduziert die Ladezeit bei vielen Keys drastisch (#25).
- 📊 **Datenbank-Optimierung**: Indexe auf `timestamp`-Spalten hinzugefügt, um Statistiken und Abfragen bei großen Datenmengen zu beschleunigen (#25).

## [1.20.0] - 2026-01-16
### Added
- 🗝️ **Key Grouping**: API-Keys im Dashboard werden nun ab 6 Keys automatisch gruppiert und können ein- bzw. ausgeklappt werden (#24).

## [1.19.9] - 2026-01-16
### Fixed
- 🕒 **Logging**: Zeitstempel zu den Docker/Uvicorn-Logs hinzugefügt für bessere Nachverfolgbarkeit.

## [1.19.8] - 2026-01-16
### Fixed
- 🙈 **Config Tracking**: `config/config.yaml` aus dem Git-Index entfernt, um das Überschreiben lokaler User-Konfigurationen zu verhindern.
- 🧪 **CI-Pipeline**: Automatisches Erstellen einer Dummy-Konfiguration für die CI-Tests hinzugefügt.
- 📝 **Fehlermeldung**: Irreführender Hinweis auf Umgebungsvariablen in der `load_keys` Fehlermeldung korrigiert.

## [1.19.6] - 2024-05-24
### Fixed
- 🔄 **Retry bei Upstream-500**: Der Proxy versucht nun automatisch einen anderen Key, wenn Ollama Cloud mit einem 500 Internal Server Error antwortet.
- ⏱️ **Kurzzeit-Penalty**: Keys, die einen 500er verursachen, werden für 30 Sekunden pausiert, um Instabilitäten abzufangen.

## [1.19.5] - 2024-05-24
### Added
- 🔍 **Massives Diagnostic Logging**: Detailliertes Step-by-Step Logging für jede Anfrage eingebaut, um die Ursache von 500er-Fehlern präzise zu identifizieren.
- 🛡️ **Erweiterte Exception-Kontrolle**: Zusätzliche try-except Blöcke für Authentifizierung und Stream-Initialisierung.

## [1.19.4] - 2024-05-24
### Fixed
- 🛡️ **Stabilität beim Key-Wechsel**: Behebung von 500er-Fehlern durch saubereres Schließen von Verbindungen (`aclose`) vor einem Retry.
- 🔍 **Detailliertes Debugging**: Einführung von Traceback-Logging bei kritischen Fehlern in der Proxy-Logik zur schnelleren Fehleranalyse.
- 🚦 **Intelligente Key-Rotation**: Erweiterung der Retry-Logik auf Upstream-Fehler (502, 503, 504) mit automatischer 30-sekündiger Abkühlphase für betroffene Keys.
- 🩹 **Fehler-Durchreichung**: Verbesserte Status-Code Behandlung; wenn alle Keys versagen, wird nun ein präziserer 503-Status oder die ursprüngliche Fehlermeldung geliefert.

## [1.18.0] - 2024-05-24
### Added
- 🤖 **Background Health Worker**: Ein interner asyncio-Task prüft nun alle 60 Sekunden automatisch, ob bestrafte Keys wieder einsatzbereit sind.
- 🛠️ Refactoring der Health-Check Logik zur nahtlosen Integration von Hintergrund-Prozessen und Dashboard-Abfragen.

## [1.17.0] - 2024-05-24
### Added
- ⏳ **Exponentielles Backoff**: Keys werden bei wiederholten Rate-Limits (`429`) progressiv länger gesperrt (15m, 1h, 2h, 6h, 12h, 24h).
- 🔄 **Manueller Reset**: Neue Schaltfläche im Dashboard, um Penalty und Backoff-Level eines Keys sofort zurückzusetzen.
- 📊 Erweiterte Key-Karten: Anzeige des aktuellen Backoff-Levels und Countdowns bis zur nächsten automatischen Prüfung.

## [1.16.0] - 2024-05-24
### ⚠️ BREAKING CHANGES
- **API-Key Konfiguration**: Die Unterstützung für API-Keys über die Umgebungsvariablen `OLLAMA_API_KEYS` und `OLLAMA_API_KEY` wurde entfernt.
- **Migration**: Alle Keys müssen nun zwingend in der `config/config.yaml` hinterlegt werden. Eine Vorlage findet sich in `config/config.template.yaml`.

### Added
- 📄 Konfigurations-Template: `config/config.template.yaml` hinzugefügt.

## [1.15.0] - 2024-05-24
### Added
- 🎨 Dashboard-Optimierung: Scrollbare Container für die Tabellen "Aggregated Stats" und "Recent Queries".
- 📌 Sticky Headers: Tabellenköpfe bleiben beim Scrollen fixiert für bessere Übersichtlichkeit.

## [1.14.4] - 2024-05-24
### Fixed
- 🐞 SQL-Fehler: Korrektur der `GROUP BY` und `ORDER BY` Klauseln im `/stats` Endpunkt nach der Umstellung auf UTC-Buckets.

## [1.14.3] - 2024-05-24
### Fixed
- 🔗 Stabilität: Umstellung auf einen globalen `AsyncClient`, um vorzeitige Verbindungsabbrüche (`ReadError`) bei Streaming-Antworten zu verhindern.
- 🛡️ Frontend-Sicherheit: Zusätzliche Array-Prüfungen im Dashboard, um Abstürze bei fehlerhaften API-Antworten zu vermeiden.

## [1.14.2] - 2024-05-24
### Fixed
- 🛡️ Robustheit: Behebung eines 500er-Fehlers bei Key-Erschöpfung; der Proxy gibt nun die korrekte Fehlermeldung des letzten Keys zurück.
- 📝 Logging: Detaillierte Log-Ausgaben für Key-Rotationen und fehlgeschlagene Versuche.

## [1.14.1] - 2024-05-24
### Fixed
- 🌍 Timezone handling: Umstellung auf ein reines UTC-Backend mit ISO 8601 Zeitstempeln und lokaler Konvertierung im Browser. Löst Probleme mit verschobenen Daten in Charts.

## [1.14.0] - 2024-05-24
### Added
- 🔄 **Automatische Key-Rotation**: Bei einem `429 Too Many Requests` wird der Request intern sofort mit einem anderen verfügbaren Key wiederholt.
- ⚖️ Erweitertes Load-Balancing: Keys werden während eines Retries intelligent ausgeschlossen, bis alle Optionen erschöpft sind.

## [1.13.2] - 2024-05-24
### Fixed
- 🐞 Daten-Replikation: Verwendung eindeutiger Minuten-Buckets (`YYYY-MM-DD HH:MM`) im Graphen, um Überschneidungen an Tagesgrenzen zu verhindern.

## [1.13.1] - 2024-05-24
### Fixed
- 📉 Chart-Fix: Korrektur der Skalierung der Summenlinie im Token-Usage-Graph.

## [1.13.0] - 2024-05-24
### Added
- ⚙️ Konfigurierbare Zeitfenster: Der Token-Usage-Graph unterstützt nun Zeiträume von 60m, 2h, 4h, 6h, 12h und 24h.

## [1.12.0] - 2024-05-24
### Added
- 📈 **Token Counter**: Neues Dashboard-Element für die Gesamtanzahl der Tokens der letzten 24 Stunden.
- 📊 Sparklines: Kleiner Hintergrund-Graph für den Token-Trend im Counter-Element.
- 🚀 Backend: Neuer Endpunkt `/stats/24h` für aggregierte Tagesstatistiken.

## [1.11.0] - 2024-05-23
### Added
- ⚖️ Initiales Load-Balancing basierend auf der Nutzung der letzten 2 Stunden.
- 🖥️ Erstes Dashboard mit API-Key Status und Token-Usage-Chart (letzte 60 Min).