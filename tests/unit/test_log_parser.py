"""Unit-Tests für den Log-Parser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_core.tools.log_parser import (
    detect_patterns,
    parse_esphome_log,
    parse_ha_core_log,
    parse_log,
    parse_zigbee2mqtt_log,
)
from models.enums import LogSource

HA_CORE_SAMPLE = """\
2024-11-15 10:23:45.123 ERROR (MainThread) [homeassistant.components.mqtt] \
Cannot connect to MQTT broker
2024-11-15 10:23:46.456 WARNING (MainThread) [homeassistant.components.zigbee2mqtt] \
Device 0x0000000000000001 is unavailable
2024-11-15 10:23:47.789 INFO (MainThread) [homeassistant.core] \
Starting Home Assistant
"""

HA_CORE_WITH_TRACEBACK = """\
2024-11-15 10:30:00.000 ERROR (MainThread) [homeassistant.components.rest] \
Error fetching data
Traceback (most recent call last):
  File "/usr/src/homeassistant/homeassistant/components/rest/__init__.py", line 42, in update
    data = await self._fetch()
  File "/usr/src/homeassistant/homeassistant/components/rest/__init__.py", line 55, in _fetch
    raise UpdateFailed("Connection refused")
UpdateFailed: Connection refused
"""

ESPHOME_SAMPLE = """\
[14:32:01][D][esp32_ble_tracker:554]: Starting scan...
[14:32:05][E][api:102]: Error while reading incoming messages
[14:32:10][W][ota:100]: OTA update failed
"""

Z2M_SAMPLE = """\
2024-11-15 10:00:01 info MQTT connected
2024-11-15 10:00:05 warn Device '0xabc123' is not reachable
2024-11-15 10:00:10 error Failed to set state for device
"""


class TestHACoreParser:
    def test_parses_error_entry(self) -> None:
        entries = parse_ha_core_log(HA_CORE_SAMPLE)
        errors = [e for e in entries if e.level == "ERROR"]
        assert len(errors) == 1
        assert "mqtt" in errors[0].message.lower()

    def test_parses_all_levels(self) -> None:
        entries = parse_ha_core_log(HA_CORE_SAMPLE)
        levels = {e.level for e in entries}
        assert "ERROR" in levels
        assert "WARNING" in levels
        assert "INFO" in levels

    def test_extracts_integration(self) -> None:
        entries = parse_ha_core_log(HA_CORE_SAMPLE)
        mqtt_entry = next((e for e in entries if e.integration == "mqtt"), None)
        assert mqtt_entry is not None

    def test_extracts_component(self) -> None:
        entries = parse_ha_core_log(HA_CORE_SAMPLE)
        assert any(e.component and "homeassistant" in e.component for e in entries)

    def test_source_is_ha_core(self) -> None:
        entries = parse_ha_core_log(HA_CORE_SAMPLE)
        assert all(e.source == LogSource.HA_CORE for e in entries)

    def test_traceback_extracted(self) -> None:
        entries = parse_ha_core_log(HA_CORE_WITH_TRACEBACK)
        error_entries = [e for e in entries if e.level == "ERROR"]
        assert len(error_entries) >= 1
        # Traceback sollte an mindestens einem Entry hängen
        has_traceback = any(e.traceback for e in error_entries)
        assert has_traceback

    def test_empty_text_returns_empty_list(self) -> None:
        assert parse_ha_core_log("") == []

    def test_timestamps_are_utc(self) -> None:
        entries = parse_ha_core_log(HA_CORE_SAMPLE)
        for entry in entries:
            assert entry.timestamp.tzinfo == UTC


class TestESPHomeParser:
    def test_parses_entries(self) -> None:
        entries = parse_esphome_log(ESPHOME_SAMPLE)
        assert len(entries) == 3

    def test_level_mapping(self) -> None:
        entries = parse_esphome_log(ESPHOME_SAMPLE)
        levels = {e.level for e in entries}
        assert "DEBUG" in levels
        assert "ERROR" in levels
        assert "WARNING" in levels

    def test_source_is_esphome(self) -> None:
        entries = parse_esphome_log(ESPHOME_SAMPLE)
        assert all(e.source == LogSource.ESPHOME for e in entries)

    def test_component_extracted(self) -> None:
        entries = parse_esphome_log(ESPHOME_SAMPLE)
        assert any(e.component == "esp32_ble_tracker" for e in entries)


class TestZigbee2MQTTParser:
    def test_parses_entries(self) -> None:
        entries = parse_zigbee2mqtt_log(Z2M_SAMPLE)
        assert len(entries) == 3

    def test_warn_mapped_to_warning(self) -> None:
        entries = parse_zigbee2mqtt_log(Z2M_SAMPLE)
        warn_entries = [e for e in entries if e.level == "WARNING"]
        assert len(warn_entries) == 1

    def test_source_is_zigbee2mqtt(self) -> None:
        entries = parse_zigbee2mqtt_log(Z2M_SAMPLE)
        assert all(e.source == LogSource.ZIGBEE2MQTT for e in entries)


class TestUniversalParser:
    def test_routes_to_correct_parser(self) -> None:
        entries = parse_log(HA_CORE_SAMPLE, LogSource.HA_CORE)
        assert all(e.source == LogSource.HA_CORE for e in entries)

    def test_fallback_for_unknown_source(self) -> None:
        # MQTT hat keinen eigenen Parser → Fallback (zeilenweise)
        entries = parse_log("some log line\nanother line", LogSource.MQTT)
        assert len(entries) >= 1


class TestPatternDetection:
    def test_detects_repeated_errors(self) -> None:
        repeated_log = "\n".join(
            [
                f"2024-11-15 10:0{i}:00.000 ERROR (MainThread) [homeassistant.components.mqtt] "
                "Cannot connect to MQTT broker"
                for i in range(6)
            ]
        )
        entries = parse_ha_core_log(repeated_log)
        patterns = detect_patterns(entries)
        repeated = [p for p in patterns if p.pattern_type == "repeated_error"]
        assert len(repeated) >= 1
        assert repeated[0].occurrences >= 5

    def test_detects_known_ha_patterns(self) -> None:
        log = (
            "2024-11-15 10:00:00.000 ERROR (MainThread) [homeassistant.loader] "
            "Integration custom_test is not loaded\n"
            "2024-11-15 10:00:01.000 ERROR (MainThread) [homeassistant.loader] "
            "Integration custom_test is not loaded\n"
            "2024-11-15 10:00:02.000 ERROR (MainThread) [homeassistant.loader] "
            "Integration custom_test is not loaded\n"
        )
        entries = parse_ha_core_log(log)
        patterns = detect_patterns(entries)
        known = [p for p in patterns if p.pattern_type == "integration_not_loaded"]
        assert len(known) >= 1

    def test_no_patterns_in_empty_log(self) -> None:
        patterns = detect_patterns([])
        assert patterns == []
