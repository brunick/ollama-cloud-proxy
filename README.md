# Ollama Cloud Proxy

Dieser Proxy leitet Anfragen an die offizielle Ollama Cloud API (`https://ollama.com/api`) weiter. Er nutzt den offiziellen API-Key zur Authentifizierung und bietet eine zusätzliche Sicherheitsschicht, um den Proxy selbst abzusichern.

## Features

- **API-Key Integration**: Nutzt den `OLLAMA_API_KEY` für die Kommunikation mit der Cloud.
- **Proxy Protection**: Optionaler `PROXY_AUTH_TOKEN`, um unbefugten Zugriff auf deinen Proxy zu verhindern.
- **Optionaler Schutz**: Kann über `ALLOW_UNAUTHENTICATED_ACCESS` auch ohne Token betrieben werden.
- **Streaming Support**: Unterstützt Streaming-Antworten (z.B. für Chat-Interfaces).
- **OpenAI & Ollama Kompatibilität**: Unterstützt Pfade mit und ohne `/api` Präfix (z.B. `/api/generate` oder `/generate`).

## Setup

1. **Konfiguration**:
   Erstelle eine `.env` Datei im Stammverzeichnis oder setze die Umgebungsvariablen direkt:
   ```env
   OLLAMA_API_KEY=dein_ollama_cloud_api_key
   
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

### Sicherheit
- Der Proxy leitet alle Pfade intelligent an `https://ollama.com/api` weiter (entfernt doppelte `/api` Präfixe automatisch).
- Wenn die Authentifizierung aktiv ist, werden Anfragen ohne gültigen `PROXY_AUTH_TOKEN` mit `401 Unauthorized` abgelehnt.
- Es werden keine lokalen Modelle gespeichert; alles läuft über die Cloud-Infrastruktur von Ollama.