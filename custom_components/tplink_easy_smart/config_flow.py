"""Config flow to configure TP-Link."""

import logging

import voluptuous as vol

from homeassistant.config_entries import CONN_CLASS_LOCAL_POLL, ConfigFlow, OptionsFlow
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client.coreapi import AuthenticationError, TpLinkWebApi
from .const import (
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PASS,
    DEFAULT_POE_STATE_SWITCHES,
    DEFAULT_PORT,
    DEFAULT_PORT_STATE_SWITCHES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SSL,
    DEFAULT_USER,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    OPT_POE_STATE_SWITCHES,
    OPT_PORT_STATE_SWITCHES,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------
#   configured_instances
# ---------------------------
@callback
def configured_instances(hass):
    """Return a set of configured instances."""
    return set(
        entry.data[CONF_NAME] for entry in hass.config_entries.async_entries(DOMAIN)
    )


# ---------------------------
#   TpLinkControllerConfigFlow
# ---------------------------
class TpLinkControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """TpLinkControllerConfigFlow class"""

    VERSION = 2
    CONNECTION_CLASS = CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return TpLinkControllerOptionsFlowHandler(config_entry)

    async def async_step_reauth(self, entry_data):
        """Recover credentials without deleting existing entities."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            api = TpLinkWebApi(
                host=data[CONF_HOST],
                port=data[CONF_PORT],
                use_ssl=data[CONF_SSL],
                user=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                verify_ssl=data[CONF_VERIFY_SSL],
            )
            try:
                await api.authenticate()
            except AuthenticationError as ex:
                errors["base"] = ex.reason_code or "auth_general"
            finally:
                await api.disconnect()
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=entry.data[CONF_USERNAME]): str,
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, user_input=None):
        """Occurs when a previous entry setup fails and is re-initiated."""
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        errors = {}
        if user_input is not None:
            # Check if instance with this name already exists
            if user_input[CONF_NAME] in configured_instances(self.hass):
                errors["base"] = "name_exists"
                return self._show_config_form(user_input=user_input, errors=errors)

            # Test connection
            api = TpLinkWebApi(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                use_ssl=user_input[CONF_SSL],
                user=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                verify_ssl=user_input[CONF_VERIFY_SSL],
            )
            try:
                await api.authenticate()
            except AuthenticationError as aex:
                errors["base"] = aex.reason_code or "auth_general"
            except Exception as ex:
                _LOGGER.warning("Setup failed: %s", {str(ex)})
                errors["base"] = "auth_general"
            finally:
                await api.disconnect()

            # Save instance
            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=user_input
                )

            return self._show_config_form(user_input=user_input, errors=errors)

        return self._show_config_form(
            user_input={
                CONF_NAME: DEFAULT_NAME,
                CONF_HOST: DEFAULT_HOST,
                CONF_USERNAME: DEFAULT_USER,
                CONF_PASSWORD: DEFAULT_PASS,
                CONF_PORT: DEFAULT_PORT,
                CONF_SSL: DEFAULT_SSL,
                CONF_VERIFY_SSL: DEFAULT_VERIFY_SSL,
            },
            errors=errors,
        )

    # ---------------------------
    #   _show_config_form
    # ---------------------------
    def _show_config_form(self, user_input, errors=None):
        """Show the configuration form to edit data."""
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=user_input[CONF_NAME]): str,
                    vol.Required(CONF_HOST, default=user_input[CONF_HOST]): str,
                    vol.Required(CONF_USERNAME, default=user_input[CONF_USERNAME]): str,
                    vol.Required(
                        CONF_PASSWORD, default=user_input[CONF_PASSWORD]
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    vol.Required(CONF_PORT, default=user_input[CONF_PORT]): cv.port,
                    vol.Required(CONF_SSL, default=user_input[CONF_SSL]): bool,
                    vol.Required(
                        CONF_VERIFY_SSL, default=user_input[CONF_VERIFY_SSL]
                    ): bool,
                }
            ),
            errors=errors,
        )


# ---------------------------
#   TpLinkControllerOptionsFlowHandler
# ---------------------------
class TpLinkControllerOptionsFlowHandler(OptionsFlow):
    """Handle options."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        return await self.async_step_basic_options(user_input)

    async def async_step_basic_options(self, user_input=None):
        """Manage the basic options options."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_features_select()

        return self.async_show_form(
            step_id="basic_options",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL,
                            self.config_entry.data.get(
                                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                            ),
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                }
            ),
        )

    async def async_step_features_select(self, user_input=None):
        """Manage the controls select options."""
        if user_input is not None:
            self.options.update(user_input)
            return self.async_create_entry(title="", data=self.options)

        return self.async_show_form(
            step_id="features_select",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_PORT_STATE_SWITCHES,
                        default=self.config_entry.options.get(
                            OPT_PORT_STATE_SWITCHES, DEFAULT_PORT_STATE_SWITCHES
                        ),
                    ): bool,
                    vol.Required(
                        OPT_POE_STATE_SWITCHES,
                        default=self.config_entry.options.get(
                            OPT_POE_STATE_SWITCHES, DEFAULT_POE_STATE_SWITCHES
                        ),
                    ): bool,
                },
            ),
        )
