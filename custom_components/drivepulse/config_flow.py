"""Choose direct webhook or authenticated Azure synchronization in the UI."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError

from .azure import (
    AzureAuthError,
    AzureClient,
    AzureError,
    azure_settings,
    normalize_api_url,
)
from .const import (
    CONF_API_URL,
    CONF_LOCAL_ONLY,
    CONF_MODE,
    CONF_READ_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_VEHICLE,
    DEFAULT_LOCAL_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODE_AZURE,
    MODE_WEBHOOK,
)


def webhook_address(hass: HomeAssistant, webhook_id: str) -> str:
    """The local address for the direct webhook mode."""
    try:
        return webhook.async_generate_url(
            hass, webhook_id, allow_external=False, allow_ip=True
        )
    except NoURLAvailableError:
        return f"http://homeassistant.local:8123/api/webhook/{webhook_id}"


def cloud_schema(defaults: dict[str, Any], *, existing: bool = False) -> vol.Schema:
    """Do not prefill secrets in an options or reauthentication form."""
    token_key = (
        vol.Optional(CONF_READ_TOKEN) if existing else vol.Required(CONF_READ_TOKEN)
    )
    return vol.Schema(
        {
            vol.Required(CONF_API_URL, default=defaults.get(CONF_API_URL, "")): str,
            token_key: selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_VEHICLE, default=defaults.get(CONF_VEHICLE, "default")
            ): vol.All(str, vol.Match(r"^[A-Za-z0-9_-]{1,64}$")),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
        }
    )


async def validate_cloud(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate with a read-only request before accepting the configuration."""
    data = dict(data)
    try:
        data[CONF_API_URL] = normalize_api_url(data[CONF_API_URL])
    except ValueError:
        return data, {CONF_API_URL: "invalid_url"}
    token = data.get(CONF_READ_TOKEN, "").strip().removeprefix("Bearer ").strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        return data, {CONF_READ_TOKEN: "invalid_auth"}
    data[CONF_READ_TOKEN] = token
    try:
        await AzureClient(
            async_get_clientsession(hass),
            data[CONF_API_URL],
            token,
            data.get(CONF_VEHICLE, "default"),
        ).read(limit=1)
    except AzureAuthError:
        return data, {"base": "invalid_auth"}
    except AzureError:
        return data, {"base": "cannot_connect"}
    return data, {}


class DrivePulseConfigFlow(ConfigFlow, domain=DOMAIN):
    """The existing webhook remains the default; Azure is an optional mode."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Optional(CONF_MODE, default=MODE_WEBHOOK): vol.In(
                            {
                                MODE_WEBHOOK: "Direct webhook",
                                MODE_AZURE: "Azure synchronization",
                            }
                        ),
                    }
                ),
            )
        if user_input.get(CONF_MODE) == MODE_AZURE:
            return await self.async_step_azure()
        webhook_id = webhook.async_generate_id()
        return self.async_create_entry(
            title="DrivePulse",
            data={CONF_WEBHOOK_ID: webhook_id},
            description_placeholders={
                "webhook_url": webhook_address(self.hass, webhook_id)
            },
        )

    async def async_step_azure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            data, errors = await validate_cloud(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title="DrivePulse (Azure)",
                    description="azure",
                    data={CONF_MODE: MODE_AZURE, **data},
                )
        return self.async_show_form(
            step_id="azure", data_schema=cloud_schema(user_input or {}), errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        defaults = azure_settings(entry)
        errors = {}
        if user_input is not None:
            data, errors = await validate_cloud(self.hass, {**defaults, **user_input})
            if not errors:
                # Clear an older token from options so it cannot override the renewed token.
                self.hass.config_entries.async_update_entry(
                    entry,
                    options={
                        k: v
                        for k, v in entry.options.items()
                        if k
                        not in (
                            CONF_READ_TOKEN,
                            CONF_API_URL,
                            CONF_VEHICLE,
                            CONF_SCAN_INTERVAL,
                        )
                    },
                )
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=cloud_schema(defaults), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DrivePulseOptionsFlow()


class DrivePulseOptionsFlow(OptionsFlow):
    """Switch modes, configure polling or refresh credentials without a second domain."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        settings = azure_settings(entry)
        if user_input is not None:
            mode = user_input.get(CONF_MODE, settings.get(CONF_MODE, MODE_WEBHOOK))
            if mode == MODE_AZURE:
                return await self.async_step_azure()
            if CONF_WEBHOOK_ID not in entry.data:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_WEBHOOK_ID: webhook.async_generate_id()},
                )
            return self.async_create_entry(
                data={
                    CONF_MODE: MODE_WEBHOOK,
                    CONF_LOCAL_ONLY: user_input.get(
                        CONF_LOCAL_ONLY, DEFAULT_LOCAL_ONLY
                    ),
                }
            )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_MODE, default=settings.get(CONF_MODE, MODE_WEBHOOK)
                    ): vol.In(
                        {
                            MODE_WEBHOOK: "Direct webhook",
                            MODE_AZURE: "Azure synchronization",
                        }
                    ),
                    vol.Required(
                        CONF_LOCAL_ONLY,
                        default=entry.options.get(CONF_LOCAL_ONLY, DEFAULT_LOCAL_ONLY),
                    ): bool,
                }
            ),
            description_placeholders={
                "webhook_url": webhook_address(self.hass, entry.data[CONF_WEBHOOK_ID])
                if CONF_WEBHOOK_ID in entry.data
                else ""
            },
        )

    async def async_step_azure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        settings = azure_settings(self.config_entry)
        errors = {}
        if user_input is not None:
            data = {**settings, **user_input}
            if not user_input.get(CONF_READ_TOKEN):
                data[CONF_READ_TOKEN] = settings.get(CONF_READ_TOKEN, "")
            data, errors = await validate_cloud(self.hass, data)
            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_MODE: MODE_AZURE,
                        **{
                            k: data[k]
                            for k in (
                                CONF_API_URL,
                                CONF_READ_TOKEN,
                                CONF_VEHICLE,
                                CONF_SCAN_INTERVAL,
                            )
                        },
                    }
                )
        return self.async_show_form(
            step_id="azure",
            data_schema=cloud_schema(
                {**settings, **(user_input or {})},
                existing=bool(settings.get(CONF_READ_TOKEN)),
            ),
            errors=errors,
        )
