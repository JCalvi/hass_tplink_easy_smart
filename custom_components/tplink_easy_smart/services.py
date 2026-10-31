"""Validated actions scoped to the target switch and its permissions."""

from dataclasses import dataclass
from enum import StrEnum
import math

import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound, Unauthorized
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .client.classes import PoePowerLimit, PoePriority
from .const import DATA_KEY_COORDINATOR, DATA_KEY_SERVICES, DOMAIN
from .helpers import generate_entity_unique_id
from .update_coordinator import TpLinkDataUpdateCoordinator

_FIELD_MAC_ADDRESS = "mac_address"
_FIELD_POWER_LIMIT = "power_limit"
_FIELD_PORT_NUMBER = "port_number"
_FIELD_ENABLED = "enabled"
_FIELD_PRIORITY = "priority"
_FIELD_MANUAL_POWER_LIMIT = "manual_power_limit"
_CV_MAC_ADDR = cv.matches_regex(r"^([A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}$")
_POE_PRIORITY_MAP = {
    "High": PoePriority.HIGH,
    "Middle": PoePriority.MIDDLE,
    "Low": PoePriority.LOW,
}
_POE_POWER_LIMIT_MAP = {
    "Auto": PoePowerLimit.AUTO,
    "Class 1": PoePowerLimit.CLASS_1,
    "Class 2": PoePowerLimit.CLASS_2,
    "Class 3": PoePowerLimit.CLASS_3,
    "Class 4": PoePowerLimit.CLASS_4,
    "Manual": None,
}


class ServiceNames(StrEnum):
    SET_GENERAL_POE_LIMIT = "set_general_poe_limit"
    SET_PORT_POE_SETTINGS = "set_port_poe_settings"


def _finite_number(value):
    if isinstance(value, bool):
        raise vol.Invalid("A finite number is required")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as ex:
        raise vol.Invalid("A finite number is required") from ex
    if not math.isfinite(number):
        raise vol.Invalid("A finite number is required")
    return number


def _port_number(value):
    if isinstance(value, bool):
        raise vol.Invalid("Port number must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as ex:
        raise vol.Invalid("Port number must be a positive integer") from ex
    if number < 1 or str(number) != str(value).strip():
        raise vol.Invalid("Port number must be a positive integer")
    return number


def _validate_manual_limit(data):
    if data[_FIELD_POWER_LIMIT] == "Manual" and _FIELD_MANUAL_POWER_LIMIT not in data:
        raise vol.Invalid("manual_power_limit is required when power_limit is Manual")
    return data


@dataclass
class ServiceDescription:
    name: str
    schema: object


SERVICES = [
    ServiceDescription(
        ServiceNames.SET_GENERAL_POE_LIMIT,
        vol.Schema(
            {
                vol.Required(_FIELD_MAC_ADDRESS): _CV_MAC_ADDR,
                vol.Required(_FIELD_POWER_LIMIT): vol.All(
                    _finite_number, vol.Range(min=1, max=1000)
                ),
            }
        ),
    ),
    ServiceDescription(
        ServiceNames.SET_PORT_POE_SETTINGS,
        vol.All(
            vol.Schema(
                {
                    vol.Required(_FIELD_MAC_ADDRESS): _CV_MAC_ADDR,
                    vol.Required(_FIELD_PORT_NUMBER): _port_number,
                    vol.Required(_FIELD_ENABLED): cv.boolean,
                    vol.Required(_FIELD_PRIORITY): vol.In(_POE_PRIORITY_MAP),
                    vol.Required(_FIELD_POWER_LIMIT): vol.In(_POE_POWER_LIMIT_MAP),
                    vol.Optional(_FIELD_MANUAL_POWER_LIMIT): vol.All(
                        _finite_number, vol.Range(min=0.1, max=30)
                    ),
                }
            ),
            _validate_manual_limit,
        ),
    ),
]


def _find_coordinator(
    hass: HomeAssistant, device_mac: str
) -> TpLinkDataUpdateCoordinator | None:
    for key, item in hass.data.get(DOMAIN, {}).items():
        if key == DATA_KEY_SERVICES or not isinstance(item, dict):
            continue
        coordinator = item.get(DATA_KEY_COORDINATOR)
        if not isinstance(coordinator, TpLinkDataUpdateCoordinator):
            continue
        info = coordinator.get_switch_info()
        if info and info.mac and info.mac.casefold() == device_mac.casefold():
            return coordinator
    return None


async def _async_authorize_target(hass, service, coordinator, port_number=None):
    """Authorize the actual target, not an arbitrary entity in this integration."""
    if service.context.user_id is None:
        return  # Internal automations retain their normal HA authorization semantics.
    user = await hass.auth.async_get_user(service.context.user_id)
    if user is None:
        raise Unauthorized(context=service.context, permission=POLICY_CONTROL)
    if user.is_admin:
        return
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, coordinator.entry_id)
    if port_number is not None:
        for function in (
            f"port_{port_number}_poe_enabled",
            f"port_{port_number}_poe_state",
        ):
            uid = generate_entity_unique_id(coordinator, function)
            targets = [entry for entry in entries if entry.unique_id == uid]
            if targets:
                entries = targets
                break
        else:
            entries = []  # No registered target: fail closed for restricted users.
    # A whole-switch change requires access to the whole target, rather than one port.
    if not entries or not all(
        user.permissions.check_entity(entry.entity_id, POLICY_CONTROL)
        for entry in entries
    ):
        raise Unauthorized(context=service.context, permission=POLICY_CONTROL)


async def _async_set_general_poe_limit(hass: HomeAssistant, service: ServiceCall):
    coordinator = _find_coordinator(hass, service.data[_FIELD_MAC_ADDRESS])
    if coordinator is None:
        raise HomeAssistantError("Cannot find switch with the specified MAC address")
    await _async_authorize_target(hass, service, coordinator)
    try:
        await coordinator.async_set_poe_limit(service.data[_FIELD_POWER_LIMIT])
    except Exception as ex:
        raise HomeAssistantError(str(ex)) from ex


async def _async_set_port_poe_settings(hass: HomeAssistant, service: ServiceCall):
    coordinator = _find_coordinator(hass, service.data[_FIELD_MAC_ADDRESS])
    if coordinator is None:
        raise HomeAssistantError("Cannot find switch with the specified MAC address")
    port_number = service.data[_FIELD_PORT_NUMBER]
    await _async_authorize_target(hass, service, coordinator, port_number)
    power_limit = _POE_POWER_LIMIT_MAP[service.data[_FIELD_POWER_LIMIT]]
    if power_limit is None:
        power_limit = service.data[_FIELD_MANUAL_POWER_LIMIT]
    try:
        await coordinator.async_set_port_poe_settings(
            port_number,
            service.data[_FIELD_ENABLED],
            _POE_PRIORITY_MAP[service.data[_FIELD_PRIORITY]],
            power_limit,
        )
    except Exception as ex:
        raise HomeAssistantError(str(ex)) from ex


async def async_setup_services(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Register once and keep an idempotent set of owning config entries."""
    owners = hass.data.setdefault(DOMAIN, {}).setdefault(DATA_KEY_SERVICES, set())
    owners.add(config_entry.entry_id)

    async def async_call_service(service):
        if service.service == ServiceNames.SET_GENERAL_POE_LIMIT:
            await _async_set_general_poe_limit(hass, service)
        elif service.service == ServiceNames.SET_PORT_POE_SETTINGS:
            await _async_set_port_poe_settings(hass, service)
        else:
            raise ServiceNotFound(DOMAIN, service.service)

    for item in SERVICES:
        if not hass.services.has_service(DOMAIN, item.name):
            hass.services.async_register(
                DOMAIN, item.name, async_call_service, schema=item.schema
            )


async def async_unload_services(hass: HomeAssistant, config_entry: ConfigEntry):
    owners = hass.data.get(DOMAIN, {}).get(DATA_KEY_SERVICES)
    if owners is None:
        return
    owners.discard(config_entry.entry_id)
    if owners:
        return
    hass.data[DOMAIN].pop(DATA_KEY_SERVICES, None)
    for service in SERVICES:
        hass.services.async_remove(DOMAIN, service.name)
