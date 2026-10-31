"""Schema, lifecycle and target-specific permission tests using HA services."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import pytest
import voluptuous as vol
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from custom_components.tplink_easy_smart import services
from custom_components.tplink_easy_smart.const import (
    DOMAIN,
    DATA_KEY_COORDINATOR,
    DATA_KEY_SERVICES,
)
from custom_components.tplink_easy_smart.helpers import generate_entity_unique_id
from custom_components.tplink_easy_smart.update_coordinator import (
    TpLinkDataUpdateCoordinator,
)
from conftest import make_entry

PORT_DATA = {
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "port_number": 1,
    "enabled": False,
    "priority": "Middle",
    "power_limit": "Auto",
}
PORT_SCHEMA = services.SERVICES[1].schema


@pytest.mark.parametrize(
    "value,expected",
    [
        (False, False),
        (True, True),
        ("false", False),
        ("off", False),
        ("0", False),
        ("true", True),
        ("on", True),
    ],
)
def test_boolean_parsing(value, expected):
    assert PORT_SCHEMA({**PORT_DATA, "enabled": value})["enabled"] is expected


@pytest.mark.parametrize(
    "data",
    [
        {"power_limit": "Manual"},
        {"power_limit": "Manual", "manual_power_limit": float("nan")},
        {"power_limit": "Manual", "manual_power_limit": float("inf")},
        {"power_limit": "Manual", "manual_power_limit": 31},
        {"port_number": 0},
        {"port_number": -1},
        {"port_number": 1.5},
        {"port_number": True},
        {"enabled": "garbage"},
    ],
)
def test_reject_invalid_service_inputs(data):
    with pytest.raises(vol.Invalid):
        PORT_SCHEMA({**PORT_DATA, **data})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True, "bad"])
def test_general_limit_validation(value):
    with pytest.raises(vol.Invalid):
        services.SERVICES[0].schema(
            {"mac_address": PORT_DATA["mac_address"], "power_limit": value}
        )


async def test_services_register_and_dispatch_without_fallthrough(hass):
    entry = make_entry()
    obj = Mock(spec=TpLinkDataUpdateCoordinator)
    obj.get_switch_info.return_value = NS(mac="aa:bb:cc:dd:ee:ff")
    obj.async_set_poe_limit = AsyncMock()
    obj.async_set_port_poe_settings = AsyncMock()
    hass.data[DOMAIN] = {entry.entry_id: {DATA_KEY_COORDINATOR: obj}}
    await services.async_setup_services(hass, entry)
    await services.async_setup_services(hass, entry)
    assert len(hass.data[DOMAIN][DATA_KEY_SERVICES]) == 1
    await hass.services.async_call(
        DOMAIN,
        "set_general_poe_limit",
        {"mac_address": PORT_DATA["mac_address"], "power_limit": 40},
        blocking=True,
    )
    obj.async_set_poe_limit.assert_awaited_once_with(40.0)
    await hass.services.async_call(
        DOMAIN,
        "set_port_poe_settings",
        {**PORT_DATA, "enabled": "false"},
        blocking=True,
    )
    assert obj.async_set_port_poe_settings.await_args.args[1] is False
    second = make_entry()
    await services.async_setup_services(hass, second)
    await services.async_unload_services(hass, entry)
    assert hass.services.has_service(DOMAIN, "set_general_poe_limit")
    await services.async_unload_services(hass, entry)  # repeated unload is harmless
    assert hass.services.has_service(DOMAIN, "set_general_poe_limit")
    await services.async_unload_services(hass, second)
    assert not hass.services.has_service(DOMAIN, "set_general_poe_limit")


async def test_permissions_are_checked_on_target_port_and_switch(hass):
    entry = make_entry()
    other = make_entry()
    for e in (entry, other):
        hass.config_entries._entries[e.entry_id] = e
    obj = Mock(spec=TpLinkDataUpdateCoordinator)
    obj.entry_id = entry.entry_id
    obj.unique_id = entry.entry_id
    obj.get_switch_info.return_value = NS(mac="aa:bb:cc:dd:ee:ff")
    registry = er.async_get(hass)
    registry.entities = er.EntityRegistryItems(hass)
    registry.deleted_entities = {}
    # Populate real registry indexes without bootstrapping unrelated HA registries.
    import attr

    def register(entity_id, uid, owner):
        kwargs = {
            f.name: None
            for f in attr.fields(er.RegistryEntry)
            if f.init and f.default is attr.NOTHING
        }
        kwargs.update(
            entity_id=entity_id,
            unique_id=uid,
            platform=DOMAIN,
            config_entry_id=owner.entry_id,
        )
        value = er.RegistryEntry(**kwargs)
        registry.entities[entity_id] = value
        return value

    target = register(
        "switch.target_port_1",
        generate_entity_unique_id(obj, "port_1_poe_enabled"),
        entry,
    )
    other_port = register(
        "switch.target_port_2",
        generate_entity_unique_id(obj, "port_2_poe_enabled"),
        entry,
    )
    foreign = register("switch.other_switch", "other_switch", other)
    allowed = {foreign.entity_id}
    user = NS(
        is_admin=False,
        permissions=NS(check_entity=lambda entity_id, policy: entity_id in allowed),
    )
    hass.auth = NS(async_get_user=AsyncMock(return_value=user))
    call = ServiceCall(
        hass,
        DOMAIN,
        "set_port_poe_settings",
        PORT_DATA,
        context=Context(user_id="restricted"),
    )
    with pytest.raises(Unauthorized):
        await services._async_authorize_target(hass, call, obj, 1)
    allowed.add(target.entity_id)
    await services._async_authorize_target(hass, call, obj, 1)
    with pytest.raises(Unauthorized):
        await services._async_authorize_target(hass, call, obj, 2)
    with pytest.raises(Unauthorized):
        await services._async_authorize_target(hass, call, obj)
    allowed.add(other_port.entity_id)
    await services._async_authorize_target(hass, call, obj)
    with pytest.raises(Unauthorized):
        await services._async_authorize_target(hass, call, obj, 99)
    hass.auth.async_get_user.return_value = None
    with pytest.raises(Unauthorized):
        await services._async_authorize_target(hass, call, obj, 1)
    hass.auth.async_get_user.return_value = NS(is_admin=True)
    await services._async_authorize_target(hass, call, obj, 1)
