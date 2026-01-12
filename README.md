# Ollama Cloud Proxy

Dieser Proxy leitet Anfragen an die offizielle Ollama Cloud API (`https://ollama.com/api`) weiter. Er nutzt den offiziellen API-Key zur Authentifizierung und bietet eine zusätzliche Sicherheitsschicht, um den Proxy selbst abzusichern.

## Features

- **Multi-Key Rotation**: Unterstützt mehrere API-Keys und wechselt automatisch bei einem `429 Too Many Requests` (Quota exceeded) zum nächsten Key.
- **Nutzungsstatistik**: Speichert Token-Verbrauch und Modell-Nutzung stündlich in einer SQLite-Datenbank.
- **API-Key Integration**: Nutzt `OLLAMA_API_KEYS` für die Kommunikation mit der Cloud.
- **Proxy Protection**: Optionaler `PROXY_AUTH_TOKEN`, um unbefugten Zugriff auf deinen Proxy zu verhindern.
- **Optionaler Schutz**: Kann über `ALLOW_UNAUTHENTICATED_ACCESS` auch ohne Token betrieben werden.
- **Streaming Support**: Unterstützt Streaming-Antworten (z.B. für Chat-Interfaces).
- **OpenAI & Ollama Kompatibilität**: Unterstützt Pfade mit und ohne `/api` Präfix (z.B. `/api/generate` oder `/generate`).

## Setup

1. **Konfiguration**:
   Erstelle eine `.env` Datei im Stammverzeichnis oder setze die Umgebungsvariablen direkt.

   ### Mehrere API-Keys (Rotation)
   Du kannst mehrere Keys über eine Konfigurationsdatei oder eine Umgebungsvariable angeben:

   **Option A: `config/config.yaml` (Empfohlen)**
   Erstelle die Datei im Projektordner:
   ```yaml
   keys:
     - "key_1"
     - "key_2"
     - "key_3"
   ```

   **Option B: Umgebungsvariable**
   ```env
   OLLAMA_API_KEYS=key1,key2,key3
   ```

   ### Weitere Optionen
   ```env
   # Option A: Abgesichert (empfohlen)
   PROXY_AUTH_TOKEN=ein_geheimes_passwort_fuer_lokal
   ALLOW_UNAUTHENTICATED_ACCESS=false
   
   # Option B: Offen (kein Token nötig)
   ALLOW_UNAUTHENTICATED_ACCESS=true
   ```
   *Deinen API-Key findest du unter [ollama.com/settings/api-keys](https://ollama.com/settings/api-keys).*

2. **Container starten**:
   ```bash
   docker-compose up -d --build
   ```

## Nutzung

Der Proxy ist unter `http://localhost:11434` erreichbar.

### Beispiel mit Authentifizierung

Wenn `ALLOW_UNAUTHENTICATED_ACCESS=false` gesetzt ist:

```bash
curl http://localhost:11434/api/generate \
  -H "Authorization: Bearer dein_geheimes_passwort_fuer_lokal" \
  -d '{
    "model": "llama3",
    "prompt": "Warum ist der Himmel blau?",
    "stream": false
  }'
```

### Beispiel ohne Authentifizierung

Wenn `ALLOW_UNAUTHENTICATED_ACCESS=true` gesetzt ist:

```bash
curl http://localhost:11434/api/generate \
  -d '{
    "model": "llama3",
    "prompt": "Warum ist der Himmel blau?",
    "stream": false
  }'
```

## Monitoring & Statistiken

Der Proxy trackt automatisch die Nutzung der API-Keys und die verbrauchten Token.

### Nutzungsstatistik abrufen (`/stats`)

Gibt eine stündliche Zusammenfassung nach Key und Modell zurück:

```bash
curl http://localhost:11434/stats
```

Beispiel-Antwort:
```json
[
  {
    "date": "2024-05-22",
    "hour": "14",
    "key_index": 0,
    "model": "llama3:8b",
    "requests": 15,
    "prompt_tokens": 1250,
    "completion_tokens": 4500
  }
]
```

### Health-Check (`/`)

Der `/` Endpunkt liefert neben dem Status auch eine Gesamtsumme der bisherigen Nutzung.

### Key-Rotation Details
- Wenn die Ollama Cloud API mit einem Status `429` (Quota exceeded) antwortet, rotiert der Proxy intern zum nächsten verfügbaren Key.
- Die ursprüngliche Anfrage wird automatisch mit dem neuen Key wiederholt.
- Der aktuell gewählte Key bleibt für nachfolgende Anfragen aktiv, bis auch dieser sein Limit erreicht.

### Sicherheit
- Der Proxy leitet alle Pfade intelligent an `https://ollama.com/api` weiter (entfernt doppelte `/api` Präfixe automatisch).
- Wenn die Authentifizierung aktiv ist, werden Anfragen ohne gültigen `PROXY_AUTH_TOKEN` mit `401 Unauthorized` abgelehnt.
- Es werden keine lokalen Modelle gespeichert; alles läuft über die Cloud-Infrastruktur von Ollama.