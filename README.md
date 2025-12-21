# Ollama Cloud Proxy

Dieser Proxy leitet Anfragen an die offizielle Ollama Cloud API (`https://ollama.com/api`) weiter. Er nutzt den offiziellen API-Key zur Authentifizierung und bietet eine zusätzliche Sicherheitsschicht, um den Proxy selbst abzusichern.

## Features

- **API-Key Integration**: Nutzt den `OLLAMA_API_KEY` für die Kommunikation mit der Cloud.
- **Proxy Protection**: Optionaler `PROXY_AUTH_TOKEN`, um unbefugten Zugriff auf deinen Proxy zu verhindern.
- **Streaming Support**: Unterstützt Streaming-Antworten (z.B. für Chat-Interfaces).
- **OpenAI & Ollama Kompatibilität**: Unterstützt Pfade mit und ohne `/api` Präfix (z.B. `/api/generate` oder `/generate`).

## Setup

1. **Konfiguration**:
   Erstelle eine `.env` Datei im Stammverzeichnis oder setze die Umgebungsvariablen direkt:
   ```env
   OLLAMA_API_KEY=dein_ollama_cloud_api_key
   PROXY_AUTH_TOKEN=ein_geheimes_passwort_fuer_lokal
   ```
   *Deinen API-Key findest du unter [ollama.com/settings/api-keys](https://ollama.com/settings/api-keys).*

2. **Container starten**:
   ```bash
   docker-compose up -d --build
   ```

## Nutzung

Der Proxy ist unter `http://localhost:11434` erreichbar.

### Beispiel mit Curl (Ollama Standard)

Wenn ein `PROXY_AUTH_TOKEN` gesetzt ist, muss dieser im Header mitgeschickt werden:

```bash
curl http://localhost:11434/api/generate \
  -H "Authorization: Bearer dein_geheimes_passwort_fuer_lokal" \
  -d '{
    "model": "llama3",
    "prompt": "Warum ist der Himmel blau?",
    "stream": false
  }'
```

### Beispiel für Chat-Completion (/api/chat)

```bash
curl http://localhost:11434/api/chat \
  -H "Authorization: Bearer dein_geheimes_passwort_fuer_lokal" \
  -d '{
    "model": "llama3",
    "messages": [
      { "role": "user", "content": "Hallo!" }
    ],
    "stream": false
  }'
```

### Sicherheit
- Der Proxy leitet alle Pfade intelligent an `https://ollama.com/api` weiter (entfernt doppelte `/api` Präfixe automatisch).
- Anfragen ohne gültigen `PROXY_AUTH_TOKEN` (falls konfiguriert) werden mit `401 Unauthorized` abgelehnt.
- Es werden keine lokalen Modelle gespeichert; alles läuft über die Cloud-Infrastruktur von Ollama.