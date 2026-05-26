"""HA Self-Healing Agent — Home Assistant Custom Integration."""

from __future__ import annotations

import logging

import voluptuous as vol
import httpx

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir

from .const import (
    ACTION_APPROVE_REPAIR,
    ACTION_REJECT_REPAIR,
    ACTION_RUN_LOG_ANALYSIS,
    ACTION_RUN_SECURITY_SCAN,
    CONF_AGENT_MODE,
    CONF_AGENT_URL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_FINDING_DETECTED,
    EVENT_REPAIR_PROPOSED,
)
from .coordinator import HAAgentCoordinator

_LOGGER = logging.getLogger(__name__)

type HASelHealingConfigEntry = ConfigEntry[HAAgentCoordinator]  # runtime_data = coordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


class AgentClient:
    """Einfacher HTTP-Client für die Kommunikation mit dem Agent-Backend."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30)

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except Exception:
            return False

    async def run_log_analysis(self, logs: list[dict]) -> dict:
        response = await self._client.post(
            "/api/v1/agents/log-analysis", json={"raw_logs": logs}
        )
        response.raise_for_status()
        return response.json()

    async def run_security_scan(self, ha_config_path: str = "/config") -> dict:
        response = await self._client.post(
            "/api/v1/security/scan",
            json={"ha_config_path": ha_config_path},
        )
        response.raise_for_status()
        return response.json()

    async def approve_repair(self, repair_action_id: str, reason: str | None = None) -> dict:
        response = await self._client.post(
            f"/api/v1/repair/{repair_action_id}/approve",
            json={"approved": True, "reason": reason},
        )
        response.raise_for_status()
        return response.json()

    async def reject_repair(self, repair_action_id: str, reason: str | None = None) -> dict:
        response = await self._client.post(
            f"/api/v1/repair/{repair_action_id}/approve",
            json={"approved": False, "reason": reason},
        )
        response.raise_for_status()
        return response.json()

    async def run_full_audit(self, ha_config_path: str = "/config") -> dict:
        response = await self._client.post(
            "/api/v1/agents/full-audit",
            json={"ha_config_path": ha_config_path},
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()


async def async_setup_entry(hass: HomeAssistant, entry: HASelHealingConfigEntry) -> bool:
    """Richtet die Integration aus einem ConfigEntry ein."""
    agent_url = entry.data[CONF_AGENT_URL]
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    # Erreichbarkeit prüfen
    client = AgentClient(agent_url)
    if not await client.health_check():
        await client.close()
        raise ConfigEntryNotReady(f"Agent unter {agent_url} nicht erreichbar")

    # Coordinator anlegen und initial abfragen
    coordinator = HAAgentCoordinator(hass, agent_url, int(scan_interval))
    coordinator.client = client  # für direkte Service-Calls
    await coordinator.async_config_entry_first_refresh()

    # Modernes HA-Pattern: runtime_data statt hass.data
    entry.runtime_data = coordinator

    # Sensor-Platform laden
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Actions/Services registrieren
    await _register_actions(hass, entry, client)

    # Auf Options-Updates reagieren
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _LOGGER.info(
        "HA Self-Healing Agent erfolgreich eingerichtet (url=%s, mode=%s)",
        agent_url,
        entry.data.get(CONF_AGENT_MODE, "advisory"),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HASelHealingConfigEntry) -> bool:
    """Deregistriert Actions und entlädt Platforms beim Entladen der Integration."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        for action in (
            ACTION_RUN_LOG_ANALYSIS,
            ACTION_RUN_SECURITY_SCAN,
            ACTION_APPROVE_REPAIR,
            ACTION_REJECT_REPAIR,
            ACTION_RUN_FULL_AUDIT,
        ):
            if hass.services.has_service(DOMAIN, action):
                hass.services.async_remove(DOMAIN, action)

        coordinator: HAAgentCoordinator = entry.runtime_data
        if client := getattr(coordinator, "client", None):
            await client.close()

    return unload_ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Lädt die Integration nach Options-Änderungen neu."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _register_actions(
    hass: HomeAssistant,
    entry: HASelHealingConfigEntry,
    client: AgentClient,
) -> None:
    """Registriert alle HA Actions (Services) der Integration."""

    async def handle_run_log_analysis(call: ServiceCall) -> None:
        logs = call.data.get("logs", [])
        try:
            result = await client.run_log_analysis(logs)
            _LOGGER.info("Log-Analyse abgeschlossen: %s Findings", result.get("findings_count", 0))

            findings_count = result.get("findings_count", 0)
            if findings_count > 0:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    f"findings_{result.get('run_id', 'unknown')}",
                    is_fixable=False,
                    issue_domain=DOMAIN,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="findings_detected",
                    translation_placeholders={"count": str(findings_count)},
                )
                hass.bus.async_fire(
                    EVENT_FINDING_DETECTED,
                    {"run_id": result.get("run_id"), "findings_count": findings_count},
                )
        except Exception as exc:
            _LOGGER.error("Log-Analyse fehlgeschlagen: %s", exc)

    async def handle_run_security_scan(call: ServiceCall) -> None:
        ha_config_path = call.data.get("ha_config_path", "/config")
        try:
            result = await client.run_security_scan(ha_config_path)
            issues_count = result.get("security_issues_count", 0)
            _LOGGER.info("Security-Scan abgeschlossen: %d Issues", issues_count)

            if issues_count > 0:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    f"security_{result.get('run_id', 'unknown')}",
                    is_fixable=False,
                    issue_domain=DOMAIN,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="findings_detected",
                    translation_placeholders={"count": str(issues_count)},
                )
        except Exception as exc:
            _LOGGER.error("Security-Scan fehlgeschlagen: %s", exc)

    async def handle_approve_repair(call: ServiceCall) -> None:
        repair_id = call.data.get("repair_action_id", "")
        reason = call.data.get("reason")
        try:
            result = await client.approve_repair(repair_id, reason)
            _LOGGER.info("Repair genehmigt: %s → %s", repair_id, result.get("decision"))
        except Exception as exc:
            _LOGGER.error("Approve fehlgeschlagen für %s: %s", repair_id, exc)

    async def handle_reject_repair(call: ServiceCall) -> None:
        repair_id = call.data.get("repair_action_id", "")
        reason = call.data.get("reason")
        try:
            result = await client.reject_repair(repair_id, reason)
            _LOGGER.info("Repair abgelehnt: %s → %s", repair_id, result.get("decision"))
        except Exception as exc:
            _LOGGER.error("Reject fehlgeschlagen für %s: %s", repair_id, exc)

    async def handle_run_full_audit(call: ServiceCall) -> None:
        ha_config_path = call.data.get("ha_config_path", "/config")
        try:
            result = await client.run_full_audit(ha_config_path)
            _LOGGER.info("Vollständiger Audit abgeschlossen: %s", result)
        except Exception as exc:
            _LOGGER.error("Full-Audit fehlgeschlagen: %s", exc)

    hass.services.async_register(
        DOMAIN,
        ACTION_RUN_LOG_ANALYSIS,
        handle_run_log_analysis,
        schema=vol.Schema({vol.Optional("logs", default=[]): list}),
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_RUN_SECURITY_SCAN,
        handle_run_security_scan,
        schema=vol.Schema({vol.Optional("ha_config_path", default="/config"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_APPROVE_REPAIR,
        handle_approve_repair,
        schema=vol.Schema({
            vol.Required("repair_action_id"): cv.string,
            vol.Optional("reason"): vol.Any(None, cv.string),
        }),
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_REJECT_REPAIR,
        handle_reject_repair,
        schema=vol.Schema({
            vol.Required("repair_action_id"): cv.string,
            vol.Optional("reason"): vol.Any(None, cv.string),
        }),
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_RUN_FULL_AUDIT,
        handle_run_full_audit,
        schema=vol.Schema({vol.Optional("ha_config_path", default="/config"): cv.string}),
    )


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HASelHealingConfigEntry
) -> dict:
    """Gibt Diagnosedaten für die Config Entry zurück (ohne sensible Daten)."""
    coordinator: HAAgentCoordinator = entry.runtime_data
    data = coordinator.data if coordinator else None

    return {
        "agent_url": entry.data[CONF_AGENT_URL],
        "mode": entry.data.get(CONF_AGENT_MODE, "advisory"),
        "scan_interval": entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        "last_update_success": coordinator.last_update_success if coordinator else False,
        "agent_reachable": data.agent_reachable if data else False,
        "agent_version": data.agent_version if data else "unknown",
        "findings_count": data.findings_count if data else 0,
        "security_issues_count": data.security_issues_count if data else 0,
        "pending_approvals": data.pending_approvals if data else 0,
        "repairs_applied": data.repairs_applied if data else 0,
    }
