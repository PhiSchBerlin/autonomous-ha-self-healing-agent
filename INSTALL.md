# Installationsanleitung — HA Self-Healing Agent

Dieses Dokument beschreibt zwei Installationsvarianten für den Raspberry Pi:

- **Option A** — Docker Compose (empfohlen, läuft neben HA OS)
- **Option B** — HA Addon (direkt im Supervisor, einfachste Integration)

---

## Voraussetzungen

| Anforderung | Minimum |
|---|---|
| Raspberry Pi | Pi 4 (4 GB RAM) oder Pi 5 |
| Home Assistant | OS 12+ oder Supervised |
| Docker | 24+ (bei Option A) |
| Speicher | 8 GB frei (für Modell-Cache) |
| Netzwerk | Pi und HA im selben LAN |

### LLM-Backend

Der Agent benötigt ein lokales LLM. Empfehlung für den Pi 4/5:

```bash
# Ollama auf dem Pi installieren
curl -fsSL https://ollama.ai/install.sh | sh

# Empfohlenes Modell laden (ca. 9 GB, läuft auf Pi 5 mit 8 GB RAM)
ollama pull qwen2.5-coder:14b

# Kleinere Alternative für Pi 4 mit 4 GB RAM
ollama pull qwen2.5-coder:7b
```

---

## Option A — Docker Compose (empfohlen)

Läuft als eigenständiger Stack neben Home Assistant OS / Supervised.

### 1. Repository klonen

```bash
ssh pi@homeassistant.local   # oder IP-Adresse
git clone https://github.com/PhiSchBerlin/autonomous-ha-self-healing-agent.git
cd ha-self-healing-agent
```

### 2. Konfiguration anlegen

```bash
cp .env.example .env
nano .env
```

Mindestens folgende Werte anpassen:

```ini
# HA Long-Lived Access Token erstellen:
# HA → Profil → Sicherheit → Langlebige Zugriffstoken → Token erstellen
HA_TOKEN=eyJ0eXAiOi...dein_token_hier

# URL deines Home Assistant (meistens schon korrekt)
HA_URL=http://homeassistant.local:8123

# Pfad zur HA-Konfiguration auf dem Host
HA_CONFIG_PATH_HOST=/usr/share/hassio/homeassistant

# LLM-Endpunkt (Ollama läuft auf dem gleichen Pi)
LLM_BASE_URL=http://host.docker.internal:11434

# Modus — für den ersten Test auf advisory lassen!
AGENT_MODE=advisory
```

> **Tipp:** Den HA-Konfigurationspfad findest du via SSH mit `ls /usr/share/hassio/homeassistant/` oder über HA → Einstellungen → System → Reparaturen → Diagnose.

### 3. Stack starten

```bash
docker compose up -d

# Logs verfolgen
docker compose logs -f agent
```

### 4. Prüfen ob alles läuft

```bash
# Agent-API
curl http://localhost:8765/health
# Erwartete Antwort: {"status":"ok","version":"0.1.0"}

# Alle Services
docker compose ps
```

### 5. Dashboard öffnen

Öffne im Browser: `http://homeassistant.local:8501`

### 6. Custom Component installieren (optional, für HA-Sensoren)

Damit HA den Agent als Integration erkennt und Sensoren anlegt:

```bash
# Custom-Components-Verzeichnis anlegen (falls nicht vorhanden)
mkdir -p /usr/share/hassio/homeassistant/custom_components

# Custom Component kopieren
cp -r ha_integration/custom_components/ha_self_healing_agent \
      /usr/share/hassio/homeassistant/custom_components/

# HA neu starten
ha core restart
```

Danach in HA: **Einstellungen → Geräte & Dienste → Integration hinzufügen → HA Self-Healing Agent**

---

## Option B — HA Addon (einfachste Integration)

Läuft direkt im HA Supervisor. Kein separater Docker-Stack nötig.

### 1. Addon-Repository in HA hinzufügen

In Home Assistant:

1. **Einstellungen → Add-ons → Add-on Store**
2. Drei-Punkte-Menü oben rechts (⋮) → **Repositories**
3. URL eintragen:
   ```
   https://github.com/PhiSchBerlin/autonomous-ha-self-healing-agent
   ```
4. **Hinzufügen** klicken → Seite neu laden

### 2. Addon installieren

1. Im Add-on Store erscheint nun **HA Self-Healing Agent**
2. **Installieren** klicken (Buildprozess dauert 5–10 Minuten)

### 3. Addon konfigurieren

Im Addon unter **Konfiguration**:

```yaml
agent_mode: advisory          # advisory | approval | autonomous
llm_backend_type: ollama
llm_model: qwen2.5-coder:14b
# Ollama auf dem gleichen Pi — Addon-interne Adresse:
llm_base_url: http://homeassistant:11434
log_level: INFO
approval_timeout_seconds: 3600
allow_autonomous_file_writes: false
```

4. **Speichern → Starten**

### 4. Prüfen

- **Addon-Log** im HA-UI zeigt Startmeldungen
- API erreichbar unter: `http://homeassistant.local:8765/health`

---

## HA Long-Lived Access Token erstellen

1. In HA einloggen
2. Unten links auf **Benutzerprofil** klicken
3. Ganz unten: **Sicherheit → Langlebige Zugriffstoken**
4. **Token erstellen** → Name z.B. `ha-self-healing-agent`
5. Token kopieren — wird nur einmal angezeigt!

---

## Erster Test nach der Installation

### Security-Scan auslösen

```bash
curl -X POST http://homeassistant.local:8765/api/v1/security/scan \
  -H "Content-Type: application/json" \
  -d '{"ha_config_path": "/ha-config"}'
```

### Log-Analyse mit echten HA-Logs

```bash
# Letzte 50 HA-Log-Zeilen holen und analysieren
curl -X POST http://homeassistant.local:8765/api/v1/agents/log-analysis \
  -H "Content-Type: application/json" \
  -d "{\"raw_logs\": $(ha core logs --no-color 2>/dev/null | tail -50 | jq -R '{raw: .}' | jq -s .)}"
```

### Dashboard öffnen

| Service | URL |
|---|---|
| Dashboard | `http://homeassistant.local:8501` |
| Agent API | `http://homeassistant.local:8765` |
| API-Dokumentation | `http://homeassistant.local:8765/docs` |
| Prometheus | `http://homeassistant.local:9090` |
| Grafana | `http://homeassistant.local:3000` |

---

## Fehlerbehebung

### Agent startet nicht

```bash
docker compose logs agent
# Häufige Ursache: HA_TOKEN fehlt oder ist ungültig
```

### Ollama nicht erreichbar

```bash
# Testen ob Ollama von Docker aus erreichbar ist
docker compose exec agent curl http://host.docker.internal:11434/api/tags
# Falls leer: ollama pull qwen2.5-coder:14b noch nicht ausgeführt
```

### ChromaDB-Verbindungsfehler

```bash
docker compose logs chromadb
# Volumes prüfen:
docker volume ls | grep ha-agent
```

### Custom Component wird nicht erkannt

```bash
# Pfad prüfen — dieser Ordner muss existieren:
ls /usr/share/hassio/homeassistant/custom_components/ha_self_healing_agent/manifest.json

# HA-Log auf Fehler prüfen:
ha core logs | grep ha_self_healing
```

### Pi 4 mit 4 GB RAM: Out of Memory

Kleineres Modell verwenden:

```bash
ollama pull qwen2.5-coder:7b
# In .env anpassen:
LLM_MODEL=qwen2.5-coder:7b
docker compose restart agent
```

---

## Update

```bash
git pull
docker compose pull
docker compose up -d
```

---

## Deinstallation

```bash
# Stack stoppen und Volumes löschen
docker compose down -v

# Custom Component entfernen
rm -rf /usr/share/hassio/homeassistant/custom_components/ha_self_healing_agent
ha core restart
```
