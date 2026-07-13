"""Button entities for TP-Link Easy Smart."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client.const import FEATURE_STATS
from .helpers import (
    generate_entity_id,
    generate_entity_name,
    generate_entity_unique_id,
    get_coordinator,
)
from .update_coordinator import TpLinkDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

_FUNCTION_NAME = "Clear port statistics"
_FUNCTION_UID = "clear_port_statistics"
ENTITY_DOMAIN = "button"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TP-Link Easy Smart buttons."""
    coordinator: TpLinkDataUpdateCoordinator = get_coordinator(hass, config_entry)

    if not await coordinator.is_feature_available(FEATURE_STATS):
        return

    async_add_entities([TpLinkClearPortStatisticsButton(coordinator)])


class TpLinkClearPortStatisticsButton(
    CoordinatorEntity[TpLinkDataUpdateCoordinator], ButtonEntity
):
    """Button that clears packet counters for every switch port."""

    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: TpLinkDataUpdateCoordinator) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        switch_info = coordinator.get_switch_info()
        device_name = switch_info.name if switch_info else coordinator.name

        self._attr_name = generate_entity_name(_FUNCTION_NAME, device_name)
        self._attr_unique_id = generate_entity_unique_id(coordinator, _FUNCTION_UID)
        self._attr_device_info = coordinator.get_device_info()
        self.entity_id = generate_entity_id(coordinator, ENTITY_DOMAIN, _FUNCTION_NAME)

    async def async_press(self) -> None:
        """Clear all port statistics and refresh coordinator data."""
        _LOGGER.debug("Clearing all port statistics")
        await self.coordinator.async_clear_port_statistics()
