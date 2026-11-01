"""Update coordinator for TP-Link."""

from datetime import timedelta
from functools import wraps
import asyncio
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
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
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
from .const import (
    ATTR_MANUFACTURER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _serialized_operation(method):
    """Keep complete actions atomic with polling and shutdown."""

    @wraps(method)
    async def run(self, *args, **kwargs):
        async with self._operation_lock:
            if self._shutdown_requested:
                raise HomeAssistantError("The switch integration is stopping")
            try:
                return await method(self, *args, **kwargs)
            finally:
                # An action's API preflight may have re-detected capabilities.
                if self._invalidate_unavailable_caches():
                    self.async_update_listeners()

    return run


# ---------------------------
#   TpLinkDataUpdateCoordinator
# ---------------------------
class TpLinkDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize."""
        self._config: ConfigEntry = config_entry
        self._operation_lock = asyncio.Lock()

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
            or not MIN_SCAN_INTERVAL <= update_interval <= MAX_SCAN_INTERVAL
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
        self._unsub_api_stop = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_on_hass_stop
        )
        config_entry.async_on_unload(self._remove_api_stop_listener)

    def _remove_api_stop_listener(self) -> None:
        """Remove the shutdown hook on unload or a failed setup attempt."""
        if self._unsub_api_stop is not None:
            unsubscribe, self._unsub_api_stop = self._unsub_api_stop, None
            unsubscribe()

    async def _async_on_hass_stop(self, event) -> None:
        # The event bus already removed this one-shot listener.
        self._unsub_api_stop = None
        await self.async_unload()

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
        available = await self._api.is_feature_available(feature)
        self._invalidate_unavailable_caches()
        return available

    def _invalidate_unavailable_caches(self) -> bool:
        """Invalidate any lost capability after either polling or action preflight."""
        changed = False
        if (
            not self._api.is_feature_available_cached(FEATURE_STATS)
            and self._port_statistics
        ):
            self._port_statistics = []
            changed = True
        if not self._api.is_feature_available_cached(FEATURE_POE):
            if self._poe_state is not None or self._port_poe_states:
                self._poe_state = None
                self._port_poe_states = []
                changed = True
        return changed

    async def async_update(self) -> None:
        """Refresh atomically; stopped coordinators never reopen their clients."""
        async with self._operation_lock:
            if self._shutdown_requested:
                return
            await self._async_update_locked()

    async def _async_update_locked(self) -> None:
        """Read and validate one coherent update while holding the operation lock."""
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
        self._remove_api_stop_listener()
        await self.async_shutdown()
        async with self._operation_lock:
            await self._safe_disconnect(self._api)

    async def _update_switch_info(self):
        """Update the switch info."""
        self._switch_info = await self._api.get_device_info()

    async def _update_port_states(self):
        """Update port states."""
        try:
            self._port_states = await self._api.get_port_states()
        except (AuthenticationError, asyncio.CancelledError):
            self._port_states = []
            raise
        except Exception as ex:
            _LOGGER.warning("Can not get port states: %s", repr(ex))
            self._port_states = []

    async def _update_port_statistics(self, *, raise_on_error: bool = False):
        """Refresh counters and invalidate the cache whenever they are unavailable."""
        try:
            if not await self.is_feature_available(FEATURE_STATS):
                self._port_statistics = []
                if raise_on_error:
                    raise ValueError("Port statistics capability is unavailable")
                return
            self._port_statistics = await self._api.get_port_statistics()
            if raise_on_error and not self._port_statistics:
                raise ValueError("No port statistics returned by the switch")
        except (AuthenticationError, asyncio.CancelledError):
            self._port_statistics = []
            raise
        except Exception as ex:
            self._port_statistics = []
            _LOGGER.warning("Cannot get port statistics: %s", repr(ex))
            if raise_on_error:
                raise

    async def _update_poe_state(self, *, raise_on_error: bool = False):
        """Refresh global PoE readings, allowing actions to report readback failures."""
        try:
            if not await self.is_feature_available(FEATURE_POE):
                self._poe_state = None
                if raise_on_error:
                    raise ValueError("PoE capability is unavailable")
                return
            self._poe_state = await self._api.get_poe_state()
            if raise_on_error and self._poe_state is None:
                raise ValueError("No PoE information returned by the switch")
        except (AuthenticationError, asyncio.CancelledError):
            self._poe_state = None
            raise
        except Exception as ex:
            self._poe_state = None
            _LOGGER.warning("Cannot get PoE state: %s", repr(ex))
            if raise_on_error:
                raise

    async def _update_port_poe_states(self, *, raise_on_error: bool = False):
        """Refresh per-port PoE readings and remove stale data on feature loss."""
        try:
            if not await self.is_feature_available(FEATURE_POE):
                self._port_poe_states = []
                if raise_on_error:
                    raise ValueError("PoE capability is unavailable")
                return
            self._port_poe_states = await self._api.get_port_poe_states()
            if raise_on_error and not self._port_poe_states:
                raise ValueError("No PoE port information returned by the switch")
        except (AuthenticationError, asyncio.CancelledError):
            self._port_poe_states = []
            raise
        except Exception as ex:
            self._port_poe_states = []
            _LOGGER.warning("Cannot get PoE port states: %s", repr(ex))
            if raise_on_error:
                raise

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

    @_serialized_operation
    async def set_port_state(
        self,
        number: int,
        enabled: bool,
        speed_config: PortSpeed,
        flow_control_config: bool,
    ) -> None:
        """Set the port state."""
        try:
            await self._api.set_port_state(
                number, enabled, speed_config, flow_control_config
            )
        except (ApiCallError, asyncio.CancelledError):
            # The switch may have applied a command whose response was lost.
            self._port_states = []
            self.async_update_listeners()
            raise

        index = number - 1
        if 0 <= index < len(self._port_states):
            self._port_states[index].enabled = enabled
            self.async_update_listeners()

    @_serialized_operation
    async def async_clear_port_statistics(self) -> None:
        """Clear all port packet counters and refresh their values."""
        try:
            await self._api.clear_port_statistics()
        except (ApiCallError, asyncio.CancelledError):
            self._port_statistics = []
            self.async_update_listeners()
            raise
        try:
            await self._update_port_statistics(raise_on_error=True)
        except Exception as ex:
            raise HomeAssistantError(
                "Port statistics were cleared, but refreshing the counters failed. "
                "They will be retried at the next scheduled update."
            ) from ex
        finally:
            self.async_update_listeners()

    @_serialized_operation
    async def async_set_poe_limit(self, limit: float) -> None:
        """Set general PoE limit."""
        try:
            await self._api.set_poe_limit(limit)
        except (ApiCallError, asyncio.CancelledError):
            self._poe_state = None
            self.async_update_listeners()
            raise
        try:
            await self._update_poe_state(raise_on_error=True)
        except Exception as ex:
            raise HomeAssistantError(
                "The PoE power limit command succeeded, but refreshing the PoE "
                "readings failed. They will be retried at the next scheduled update."
            ) from ex
        finally:
            self.async_update_listeners()

    @_serialized_operation
    async def async_set_port_poe_settings(
        self,
        port_number: int,
        enabled: bool,
        priority: PoePriority,
        power_limit: PoePowerLimit | float,
    ) -> None:
        """Set the port PoE settings."""
        try:
            await self._api.set_port_poe_settings(
                port_number, enabled, priority, power_limit
            )
        except (ApiCallError, asyncio.CancelledError):
            self._port_poe_states = []
            self.async_update_listeners()
            raise
        try:
            await self._update_port_poe_states(raise_on_error=True)
        except Exception as ex:
            raise HomeAssistantError(
                "The port PoE settings command succeeded, but refreshing the port "
                "readings failed. They will be retried at the next scheduled update."
            ) from ex
        finally:
            self.async_update_listeners()
