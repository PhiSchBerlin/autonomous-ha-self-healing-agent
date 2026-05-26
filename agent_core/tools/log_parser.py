"""
Log-Parser und Normalisierer für alle Home Assistant Log-Quellen.

Konvertiert rohe Log-Strings aus verschiedenen Quellen in normalisierte
LogEntry-Objekte, die vom Log-Analyse-Agenten verarbeitet werden können.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from models.enums import LogSource
from models.log_models import LogEntry, LogPattern

# Regex-Patterns für verschiedene Log-Formate
_HA_CORE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"\((?P<thread>[^)]+)\)\s+"
    r"\[(?P<component>[^\]]+)\]\s+"
    r"(?P<message>.+)$",
    re.MULTILINE,
)

_SUPERVISOR_PATTERN = re.compile(
    r"^(?P<timestamp>\d{2}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<component>\S+):\s+"
    r"(?P<message>.+)$",
    re.MULTILINE,
)

_DOCKER_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\s+"
    r"(?P<message>.+)$",
    re.MULTILINE,
)

_ESPHOME_PATTERN = re.compile(
    r"^\[(?P<timestamp>\d{2}:\d{2}:\d{2})\]\[(?P<level>[A-Z])\s*\]\[(?P<component>[^\]]+)\]:\s+"
    r"(?P<message>.+)$",
    re.MULTILINE,
)

_ESPHOME_LEVEL_MAP = {"D": "DEBUG", "I": "INFO", "W": "WARNING", "E": "ERROR", "C": "CRITICAL"}

_ZIGBEE2MQTT_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>debug|info|warn|error)\s+"
    r"(?P<message>.+)$",
    re.MULTILINE,
)

_PYTHON_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE)
_PYTHON_TRACEBACK_END = re.compile(r"^(\w+Error|\w+Exception|Exception): .+$", re.MULTILINE)

_LEVEL_MAP = {
    "debug": "DEBUG",
    "info": "INFO",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}


def _normalize_level(raw: str) -> str:
    return _LEVEL_MAP.get(raw.lower(), raw.upper())


def _parse_timestamp(raw: str, fmt: str | None = None) -> datetime:
    """Versucht einen Zeitstempel zu parsen, gibt now() bei Fehler zurück."""
    formats = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%H:%M:%S",
    ]
    if fmt:
        formats.insert(0, fmt)
    for f in formats:
        try:
            return datetime.strptime(raw.strip(), f).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.now(UTC)


def _extract_tracebacks(text: str) -> list[str]:
    """Extrahiert alle Python-Tracebacks aus einem Log-Text."""
    tracebacks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if _PYTHON_TRACEBACK_START.match(lines[i]):
            tb_lines = [lines[i]]
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or _PYTHON_TRACEBACK_END.match(lines[i])):
                tb_lines.append(lines[i])
                if _PYTHON_TRACEBACK_END.match(lines[i]):
                    break
                i += 1
            tracebacks.append("\n".join(tb_lines))
        i += 1
    return tracebacks


def parse_ha_core_log(raw_text: str, source: LogSource = LogSource.HA_CORE) -> list[LogEntry]:
    """Parst Home Assistant Core Logs."""
    entries: list[LogEntry] = []
    pattern = _HA_CORE_PATTERN if source == LogSource.HA_CORE else _SUPERVISOR_PATTERN

    for match in pattern.finditer(raw_text):
        g = match.groupdict()
        level = _normalize_level(g.get("level", "INFO"))
        message = g.get("message", "").strip()
        component_raw = g.get("component", "")

        # Integrationsname aus Component extrahieren (z.B. "homeassistant.components.mqtt")
        integration = None
        if "components." in component_raw:
            parts = component_raw.split("components.")
            if len(parts) > 1:
                integration = parts[1].split(".")[0]

        entries.append(
            LogEntry(
                source=source,
                level=level,
                message=message,
                raw=match.group(0),
                timestamp=_parse_timestamp(g.get("timestamp", "")),
                component=component_raw or None,
                integration=integration,
            )
        )

    # Tracebacks nachträglich den passenden Einträgen zuordnen
    tracebacks = _extract_tracebacks(raw_text)
    tb_index = 0
    for entry in entries:
        if entry.level in ("ERROR", "CRITICAL") and tb_index < len(tracebacks):
            entry.traceback = tracebacks[tb_index]
            tb_index += 1

    return entries


def parse_supervisor_log(raw_text: str) -> list[LogEntry]:
    return parse_ha_core_log(raw_text, source=LogSource.SUPERVISOR)


def parse_esphome_log(raw_text: str) -> list[LogEntry]:
    """Parst ESPHome Logs."""
    entries: list[LogEntry] = []
    for match in _ESPHOME_PATTERN.finditer(raw_text):
        g = match.groupdict()
        raw_level = g.get("level", "I")
        level = _ESPHOME_LEVEL_MAP.get(raw_level.upper(), "INFO")
        component_raw = g.get("component", "")
        # ESPHome-Komponenten enthalten manchmal ":NNN" Suffix — abschneiden
        component = component_raw.split(":")[0].strip() if component_raw else None
        entries.append(
            LogEntry(
                source=LogSource.ESPHOME,
                level=level,
                message=g.get("message", "").strip(),
                raw=match.group(0),
                timestamp=_parse_timestamp(g.get("timestamp", "")),
                component=component,
            )
        )
    return entries


def parse_zigbee2mqtt_log(raw_text: str) -> list[LogEntry]:
    """Parst Zigbee2MQTT Logs."""
    entries: list[LogEntry] = []
    for match in _ZIGBEE2MQTT_PATTERN.finditer(raw_text):
        g = match.groupdict()
        entries.append(
            LogEntry(
                source=LogSource.ZIGBEE2MQTT,
                level=_normalize_level(g.get("level", "INFO")),
                message=g.get("message", "").strip(),
                raw=match.group(0),
                timestamp=_parse_timestamp(g.get("timestamp", "")),
            )
        )
    return entries


def parse_docker_log(raw_text: str) -> list[LogEntry]:
    """Parst Docker-Logs (JSON oder Plaintext)."""
    import json

    entries: list[LogEntry] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            msg = data.get("log", data.get("message", line)).strip()
            ts_raw = data.get("time", data.get("timestamp", ""))
            level = _normalize_level(data.get("level", "INFO"))
            entries.append(
                LogEntry(
                    source=LogSource.DOCKER,
                    level=level,
                    message=msg,
                    raw=line,
                    timestamp=_parse_timestamp(ts_raw),
                )
            )
        except (json.JSONDecodeError, KeyError):
            m = _DOCKER_PATTERN.match(line)
            if m:
                g = m.groupdict()
                entries.append(
                    LogEntry(
                        source=LogSource.DOCKER,
                        level="INFO",
                        message=g.get("message", "").strip(),
                        raw=line,
                        timestamp=_parse_timestamp(g.get("timestamp", "")),
                    )
                )
    return entries


_PARSERS: dict[LogSource, Any] = {
    LogSource.HA_CORE: parse_ha_core_log,
    LogSource.SUPERVISOR: parse_supervisor_log,
    LogSource.ESPHOME: parse_esphome_log,
    LogSource.ZIGBEE2MQTT: parse_zigbee2mqtt_log,
    LogSource.DOCKER: parse_docker_log,
}


def parse_log(raw_text: str, source: LogSource) -> list[LogEntry]:
    """Universeller Einstiegspunkt: wählt den richtigen Parser anhand der Quelle."""
    parser = _PARSERS.get(source)
    if parser is None:
        # Fallback: einfache zeilenweise Verarbeitung
        return [
            LogEntry(
                source=source,
                level="INFO",
                message=line.strip(),
                raw=line,
                timestamp=datetime.now(UTC),
            )
            for line in raw_text.splitlines()
            if line.strip()
        ]
    return parser(raw_text)


def detect_patterns(entries: list[LogEntry]) -> list[LogPattern]:
    """
    Erkennt wiederkehrende Muster und Regressionen in einer Liste von LogEntries.

    Sucht nach:
    - Wiederholten gleichen Fehlermeldungen (> 3x in einem Lauf)
    - Bekannten HA-Fehlermustern (Integration unavailable, Template errors usw.)
    """
    from collections import Counter, defaultdict

    patterns: list[LogPattern] = []
    error_entries = [e for e in entries if e.level in ("ERROR", "CRITICAL", "WARNING")]

    # Häufigkeitsanalyse über normalisierte Nachrichten
    msg_counter: Counter[str] = Counter()
    msg_to_entries: dict[str, list[LogEntry]] = defaultdict(list)

    for entry in error_entries:
        # Normalisiere: Zahlen, UUIDs, Pfade herausfiltern für Vergleich
        normalized = re.sub(r"\b\w*\d\w*\b", "<NUM>", entry.message)
        normalized = re.sub(r"/[^\s]+", "<PATH>", normalized)
        normalized = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<UUID>", normalized)
        msg_counter[normalized] += 1
        msg_to_entries[normalized].append(entry)

    for msg, count in msg_counter.items():
        if count >= 3:
            affected = msg_to_entries[msg]
            patterns.append(
                LogPattern(
                    pattern_type="repeated_error",
                    description=f"Wiederholter Fehler ({count}x): {msg[:120]}",
                    occurrences=count,
                    first_seen=min(e.timestamp for e in affected),
                    last_seen=max(e.timestamp for e in affected),
                    affected_entries=[e.id for e in affected],
                    confidence=min(0.5 + count * 0.05, 0.99),
                )
            )

    # Bekannte HA-Fehlermuster
    known_patterns = [
        (re.compile(r"Integration .+ is not loaded", re.I), "integration_not_loaded"),
        (re.compile(r"Error rendering template", re.I), "template_error"),
        (re.compile(r"Can't connect to", re.I), "connection_failure"),
        (re.compile(r"Setup of .+ is taking over", re.I), "slow_setup"),
        (re.compile(r"Platform .+ does not generate unique IDs", re.I), "missing_unique_id"),
        (re.compile(r"Exception in .+ when fetching", re.I), "fetch_exception"),
    ]

    for pattern, pattern_type in known_patterns:
        matched = [e for e in error_entries if pattern.search(e.message)]
        if matched:
            patterns.append(
                LogPattern(
                    pattern_type=pattern_type,
                    description=f"Bekanntes HA-Muster '{pattern_type}' ({len(matched)}x erkannt)",
                    occurrences=len(matched),
                    first_seen=min(e.timestamp for e in matched),
                    last_seen=max(e.timestamp for e in matched),
                    affected_entries=[e.id for e in matched],
                    confidence=0.85,
                )
            )

    return patterns
