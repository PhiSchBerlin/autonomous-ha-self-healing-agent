"""Sensor-Entitäten für den HA Self-Healing Agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AgentCoordinatorData, HAAgentCoordinator


@dataclass(frozen=True, kw_only=True)
class HAAgentSensorDescription(SensorEntityDescription):
    """Beschreibung eines HA Self-Healing Agent Sensors."""

    value_fn: Any = None


SENSOR_DESCRIPTIONS: tuple[HAAgentSensorDescription, ...] = (
    HAAgentSensorDescription(
        key="agent_status",
        name="Agent Status",
        icon="mdi:robot",
        value_fn=lambda d: "online" if d.agent_reachable else "offline",
    ),
    HAAgentSensorDescription(
        key="agent_mode",
        name="Agent Modus",
        icon="mdi:cog",
        value_fn=lambda d: d.agent_mode,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HAAgentSensorDescription(
        key="agent_version",
        name="Agent Version",
        icon="mdi:tag",
        value_fn=lambda d: d.agent_version,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    HAAgentSensorDescription(
        key="findings_count",
        name="Erkannte Findings",
        icon="mdi:alert-circle",
        native_unit_of_measurement="Findings",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.findings_count,
    ),
    HAAgentSensorDescription(
        key="critical_findings",
        name="Kritische Findings",
        icon="mdi:alert",
        native_unit_of_measurement="Findings",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.critical_findings,
    ),
    HAAgentSensorDescription(
        key="security_issues",
        name="Security Issues",
        icon="mdi:shield-alert",
        native_unit_of_measurement="Issues",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.security_issues_count,
    ),
    HAAgentSensorDescription(
        key="risk_score",
        name="Risiko-Score",
        icon="mdi:gauge",
        native_unit_of_measurement="Score",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.overall_risk_score, 1),
    ),
    HAAgentSensorDescription(
        key="pending_approvals",
        name="Ausstehende Genehmigungen",
        icon="mdi:clock-alert",
        native_unit_of_measurement="Approvals",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.pending_approvals,
    ),
    HAAgentSensorDescription(
        key="repairs_applied",
        name="Angewendete Reparaturen",
        icon="mdi:wrench-check",
        native_unit_of_measurement="Reparaturen",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.repairs_applied,
    ),
    HAAgentSensorDescription(
        key="knowledge_entries",
        name="Wissensdatenbank-Einträge",
        icon="mdi:brain",
        native_unit_of_measurement="Einträge",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.knowledge_entries,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Richtet Sensor-Entitäten für einen ConfigEntry ein."""
    coordinator: HAAgentCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        HAAgentSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class HAAgentSensor(CoordinatorEntity[HAAgentCoordinator], SensorEntity):
    """Repräsentiert einen Sensor-Wert des HA Self-Healing Agent."""

    entity_description: HAAgentSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HAAgentCoordinator,
        description: HAAgentSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.agent_url}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.agent_url)},
            "name": "HA Self-Healing Agent",
            "manufacturer": "HA Self-Healing Agent",
            "model": "Autonomous AI Agent",
            "sw_version": coordinator.data.agent_version if coordinator.data else "unknown",
        }

    @property
    def native_value(self) -> Any:
        """Gibt den aktuellen Sensor-Wert zurück."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Sensor verfügbar wenn Coordinator-Daten vorhanden."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
        )
