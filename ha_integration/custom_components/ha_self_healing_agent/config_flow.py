"""Config Flow für die HA Self-Healing Agent Integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    AGENT_MODES,
    CONF_AGENT_MODE,
    CONF_AGENT_URL,
    CONF_SCAN_INTERVAL,
    DEFAULT_AGENT_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


async def _validate_connection(hass: HomeAssistant, agent_url: str) -> str | None:
    """Prüft ob der Agent unter der angegebenen URL erreichbar ist."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{agent_url}/health")
            response.raise_for_status()
            return None
    except httpx.ConnectError:
        return "cannot_connect"
    except httpx.HTTPStatusError:
        return "invalid_response"
    except Exception:
        return "unknown"


class HASelHealingAgentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow für den HA Self-Healing Agent."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await _validate_connection(self.hass, user_input[CONF_AGENT_URL])
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_AGENT_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="HA Self-Healing Agent",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AGENT_URL, default=DEFAULT_AGENT_URL): TextSelector(),
                    vol.Required(CONF_AGENT_MODE, default="advisory"): SelectSelector(
                        SelectSelectorConfig(
                            options=AGENT_MODES,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
                        NumberSelectorConfig(
                            min=60,
                            max=86400,
                            step=60,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return HASelHealingAgentOptionsFlow(config_entry)


class HASelHealingAgentOptionsFlow(config_entries.OptionsFlow):
    """Options Flow zum Anpassen der Integration nach der Ersteinrichtung."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AGENT_MODE,
                        default=self._config_entry.options.get(CONF_AGENT_MODE, "advisory"),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=AGENT_MODES,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self._config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(min=60, max=86400, step=60, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
        )
