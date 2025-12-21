# Ollama Cloud Proxy

Dieser Proxy ermöglicht es, die Ollama Cloud API über ein lokales Interface anzusprechen. Da Ollama das OpenAI API Format unterstützt, kann dieser Container als Brücke dienen.

## Setup

1. **Container starten:**
   ```bash
   docker-compose up -d
   ```

2. **Authentifizierung:**
   Um den Container bei der Ollama Cloud anzumelden, musst du dich einmalig in den laufenden Container einwählen:
   ```bash
   docker-compose exec ollama-cloud-proxy ollama user signin
   ```
   Folge den Anweisungen im Terminal. Die Zugangsdaten werden im Docker Volume `ollama_data` gespeichert, sodass sie auch nach einem Neustart erhalten bleiben.

## Nutzung

Der Proxy ist nun unter `http://localhost:11434` erreichbar. Du kannst nun Modelle direkt von der Cloud nutzen oder lokal ziehen, während die Authentifizierung über den Container läuft.