"""Update coordinator for TP-Link."""

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.exceptions import ConfigEntryAuthFailed
from .client.coreapi import AuthenticationError, ApiCallError, AUTH_FAILURE_CREDENTIALS
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client.classes import PoePowerLimit, PoePriority, TpLinkSystemInfo
from .client.const import FEATURE_POE, FEATURE_STATS
from .client.tplink_api import (
    PoeState,
    PortPoeState,
    PortSpeed,
    PortState,
    TpLinkApi,
    PortStatistics,
)
from .const import ATTR_MANUFACTURER, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


# ---------------------------
#   TpLinkDataUpdateCoordinator
# ---------------------------
class TpLinkDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize."""
        self._config: ConfigEntry = config_entry

        self._api: TpLinkApi = TpLinkApi(
            host=config_entry.data[CONF_HOST],
            port=config_entry.data[CONF_PORT],
            use_ssl=config_entry.data[CONF_SSL],
            user=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
            verify_ssl=config_entry.data[CONF_VERIFY_SSL],
        )
        self._switch_info: TpLinkSystemInfo | None = None
        self._port_states: list[PortState] = []
        self._port_poe_states: list[PortPoeState] = []
        self._poe_state: PoeState | None = None
        self._port_statistics: list[PortStatistics] = []

        update_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL,
            config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        if (
            isinstance(update_interval, bool)
            or not isinstance(update_interval, int)
            or update_interval < 1
        ):
            _LOGGER.warning(
                "Invalid update interval; using %s seconds", DEFAULT_SCAN_INTERVAL
            )
            update_interval = DEFAULT_SCAN_INTERVAL

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=config_entry.data[CONF_NAME],
            update_method=self.async_update,
            update_interval=timedelta(seconds=update_interval),
        )

    @property
    def entry_id(self) -> str:
        """Return the owning configuration entry ID."""
        return self._config.entry_id

    @property
    def unique_id(self) -> str:
        """Return the system descriptor."""
        entry = self.config_entry

        if entry.unique_id:
            return entry.unique_id

        return entry.entry_id

    @property
    def cfg_host(self) -> str:
        """Return the host of the device."""
        return self.config_entry.data[CONF_HOST]

    @property
    def ports_count(self) -> int:
        """Return ports count of the device."""
        return len(self._port_states)

    @property
    def ports_poe_count(self) -> int:
        """Return PoE ports count of the device."""
        return len(self._port_poe_states)

    def get_port_state(self, number: int) -> PortState | None:
        """Return the specified port state."""
        if number > self.ports_count or number < 1:
            return None
        return self._port_states[number - 1]

    def get_port_statistics(self, number: int) -> PortStatistics | None:
        """Return the specified port statistics."""
        if (
            number > self.ports_count
            or number < 1
            or len(self._port_statistics) < number
        ):
            return None
        return self._port_statistics[number - 1]

    def get_port_poe_state(self, number: int) -> PortPoeState | None:
        """Return the specified port PoE state."""
        if number > self.ports_poe_count or number < 1:
            return None
        return self._port_poe_states[number - 1]

    def get_switch_info(self) -> TpLinkSystemInfo | None:
        """Return the information of the switch."""
        return self._switch_info

    def get_poe_state(self) -> PoeState | None:
        """Return the switch PoE state."""
        return self._poe_state

    async def _safe_disconnect(self, api: TpLinkApi) -> None:
        """Disconnect from API."""
        try:
            await api.disconnect()
        except Exception as ex:
            _LOGGER.warning("Can not schedule disconnect: %s", str(ex))

    def is_feature_available_cached(self, feature: str) -> bool:
        """Return the latest detected capability without making a request."""
        return self._api.is_feature_available_cached(feature)

    async def is_feature_available(self, feature: str) -> bool:
        """Return true if specified feature is known and available."""
        return await self._api.is_feature_available(feature)

    async def async_update(self) -> None:
        """Asynchronous update of all data."""
        try:
            _LOGGER.debug("Update started")
            await self._update_switch_info()
            await self._update_port_states()
            if not self._port_states:
                raise UpdateFailed("No valid port states returned by the switch")
            await self._update_poe_state()
            await self._update_port_poe_states()
            await self._update_port_statistics()
            _LOGGER.debug("Update completed")
        except AuthenticationError as ex:
            if ex.reason_code == AUTH_FAILURE_CREDENTIALS:
                raise ConfigEntryAuthFailed("Switch credentials were rejected") from ex
            raise UpdateFailed(
                "Switch authentication is temporarily unavailable"
            ) from ex
        except (ApiCallError, ValueError, TypeError) as ex:
            raise UpdateFailed(str(ex)) from ex

    async def async_unload(self) -> None:
        """Unload the coordinator and disconnect from API."""
        await self._safe_disconnect(self._api)

    async def _update_switch_info(self):
        """Update the switch info."""
        self._switch_info = await self._api.get_device_info()

    async def _update_port_states(self):
        """Update port states."""
        try:
            self._port_states = await self._api.get_port_states()
        except Exception as ex:
            _LOGGER.warning("Can not get port states: %s", repr(ex))
            self._port_states = []

    async def _update_port_statistics(self, *, raise_on_error: bool = False):
        """Update port statistis."""
        if not await self.is_feature_available(FEATURE_STATS):
            return

        try:
            self._port_statistics = await self._api.get_port_statistics()
            if raise_on_error and not self._port_statistics:
                raise ValueError("No port statistics returned by the switch")
        except Exception as ex:
            _LOGGER.warning("Can not get port statistics: %s", repr(ex))
            self._port_statistics = []
            if raise_on_error:
                raise

    async def _update_poe_state(self):
        """Update the switch PoE state."""

        if not await self.is_feature_available(FEATURE_POE):
            return

        try:
            self._poe_state = await self._api.get_poe_state()
        except Exception as ex:
            _LOGGER.warning("Can not get poe state: %s", repr(ex))
            self._poe_state = None

    async def _update_port_poe_states(self):
        """Update port PoE states."""

        if not await self.is_feature_available(FEATURE_POE):
            return

        try:
            self._port_poe_states = await self._api.get_port_poe_states()
        except Exception as ex:
            _LOGGER.warning("Can not get port poe states: %s", repr(ex))
            self._port_poe_states = []

    def get_device_info(self) -> DeviceInfo | None:
        """Return the DeviceInfo."""
        switch_info = self.get_switch_info()
        if not switch_info:
            _LOGGER.debug("Device info not found")
            return None

        result = DeviceInfo(
            configuration_url=self._api.device_url,
            identifiers={(DOMAIN, switch_info.mac)},
            manufacturer=ATTR_MANUFACTURER,
            name=switch_info.name,
            hw_version=switch_info.hardware,
            sw_version=switch_info.firmware,
        )
        return result

    async def set_port_state(
        self,
        number: int,
        enabled: bool,
        speed_config: PortSpeed,
        flow_control_config: bool,
    ) -> None:
        """Set the port state."""
        await self._api.set_port_state(
            number, enabled, speed_config, flow_control_config
        )

        index = number - 1
        if 0 <= index < len(self._port_states):
            self._port_states[index].enabled = enabled
            self.async_update_listeners()

    async def async_clear_port_statistics(self) -> None:
        """Clear all port packet counters and refresh their values."""
        await self._api.clear_port_statistics()
        try:
            await self._update_port_statistics(raise_on_error=True)
        except Exception as ex:
            raise HomeAssistantError(
                "Port statistics were cleared, but refreshing the counters failed. "
                "They will be retried at the next scheduled update."
            ) from ex
        finally:
            self.async_update_listeners()

    async def async_set_poe_limit(self, limit: float) -> None:
        """Set general PoE limit."""
        await self._api.set_poe_limit(limit)
        await self._update_poe_state()
        self.async_update_listeners()

    async def async_set_port_poe_settings(
        self,
        port_number: int,
        enabled: bool,
        priority: PoePriority,
        power_limit: PoePowerLimit | float,
    ) -> None:
        """Set the port PoE settings."""
        await self._api.set_port_poe_settings(
            port_number, enabled, priority, power_limit
        )
        await self._update_port_poe_states()
        self.async_update_listeners()
