"""Regressions for feature loss, mid-refresh authentication and action readback."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.tplink_easy_smart import binary_sensor, sensor, switch
from custom_components.tplink_easy_smart.client.classes import (
    PoePowerLimit,
    PoePriority,
)
from custom_components.tplink_easy_smart.client.const import FEATURE_POE
from custom_components.tplink_easy_smart.client.coreapi import AuthenticationError
from test_homeassistant import coordinator_for


async def test_capability_loss_clears_values_and_recovery_restores_them(
    hass, switch_server
):
    api, state, _ = switch_server
    # Keep probes retryable initially, while the switch has valid PoE data.
    state["status"]["PortStatisticsRpm.htm"] = 500
    coordinator, entry = coordinator_for(
        hass, switch_server, port_state_switches=True, poe_state_switches=True
    )
    await coordinator.async_config_entry_first_refresh()
    assert coordinator.get_poe_state() is not None
    assert coordinator.ports_poe_count == 2
    entities = []
    for module in (binary_sensor, sensor, switch):
        await module.async_setup_entry(hass, entry, entities.extend)
    for entity in entities:
        entity.hass = hass
        entity.async_write_ha_state = Mock()
        entity._handle_coordinator_update()

    # A subsequent complete probe explicitly loses PoE support.
    state["status"] = {"PoeConfigRpm.htm": 404}
    api._features_retry_at = 0
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.get_poe_state() is None
    assert coordinator.ports_poe_count == 0
    assert coordinator.get_port_statistics(1) is not None
    for entity in entities:
        entity._handle_coordinator_update()
    poe_entities = [
        e
        for e in entities
        if isinstance(
            e,
            (
                sensor.TpLinkPoeInfoSensor,
                binary_sensor.TpLinkPortPoeStateBinarySensor,
                switch.TpLinkPortPoeStateSwitch,
            ),
        )
    ]
    assert poe_entities and all(not e.available for e in poe_entities)

    # Lose statistics too: counter attributes must disappear, not remain frozen.
    state["status"]["PortStatisticsRpm.htm"] = 404
    api._features_retry_at = 0
    await coordinator.async_refresh()
    assert coordinator._port_statistics == []
    for entity in entities:
        entity._handle_coordinator_update()
    links = [
        e for e in entities if isinstance(e, binary_sensor.TpLinkPortStateBinarySensor)
    ]
    assert all(
        not any(k.endswith("_packets") for k in e.extra_state_attributes) for e in links
    )

    state["status"].clear()
    api._features_retry_at = 0
    await coordinator.async_refresh()
    for entity in entities:
        entity._handle_coordinator_update()
    assert all(e.available for e in poe_entities)
    assert all("tx_good_packets" in e.extra_state_attributes for e in links)
    await entry._async_process_on_unload(hass)
    await coordinator.async_unload()


@pytest.mark.parametrize(
    "method",
    ["get_port_states", "get_poe_state", "get_port_poe_states", "get_port_statistics"],
)
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("auth_invalid_credentials", ConfigEntryAuthFailed),
        ("auth_session_timeout", UpdateFailed),
    ],
)
async def test_authentication_failure_after_device_info_reaches_coordinator(
    hass, switch_server, method, reason, expected
):
    coordinator, _ = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    setattr(
        coordinator._api,
        method,
        AsyncMock(side_effect=AuthenticationError("test failure", reason)),
    )
    with pytest.raises(expected):
        await coordinator.async_update()
    await coordinator.async_unload()


@pytest.mark.parametrize(
    "helper,cache,empty",
    [
        ("_update_port_statistics", "_port_statistics", []),
        ("_update_poe_state", "_poe_state", None),
        ("_update_port_poe_states", "_port_poe_states", []),
    ],
)
async def test_failed_capability_authentication_also_invalidates_cache(
    hass, switch_server, helper, cache, empty
):
    coordinator, _ = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    coordinator.is_feature_available = AsyncMock(
        side_effect=AuthenticationError("test failure", "auth_invalid_credentials")
    )
    with pytest.raises(AuthenticationError):
        await getattr(coordinator, helper)()
    assert getattr(coordinator, cache) == empty
    await coordinator.async_unload()


@pytest.mark.parametrize("kind", ["global", "port"])
@pytest.mark.parametrize("failure", ["read", "empty", "feature", "feature_auth"])
async def test_poe_write_readback_failure_is_reported_and_notified(
    hass, switch_server, kind, failure
):
    coordinator, _ = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_update_listeners = Mock()
    if kind == "global":
        coordinator._api.set_poe_limit = AsyncMock()
        getter = "get_poe_state"
        cache, empty = "_poe_state", None
        action = coordinator.async_set_poe_limit
        args = (40,)
    else:
        coordinator._api.set_port_poe_settings = AsyncMock()
        getter = "get_port_poe_states"
        cache, empty = "_port_poe_states", []
        action = coordinator.async_set_port_poe_settings
        args = (1, False, PoePriority.MIDDLE, PoePowerLimit.AUTO)
    if failure == "read":
        setattr(
            coordinator._api,
            getter,
            AsyncMock(side_effect=RuntimeError("readback failed")),
        )
    elif failure == "empty":
        setattr(coordinator._api, getter, AsyncMock(return_value=empty))
    elif failure == "feature":
        coordinator.is_feature_available = AsyncMock(return_value=False)
    else:
        coordinator.is_feature_available = AsyncMock(
            side_effect=AuthenticationError(
                "credentials rejected", "auth_invalid_credentials"
            )
        )
    with pytest.raises(
        HomeAssistantError, match="command succeeded, but refreshing"
    ) as ex:
        await action(*args)
    assert ex.value.__cause__ is not None
    assert getattr(coordinator, cache) == empty
    coordinator.async_update_listeners.assert_called_once()
    await coordinator.async_unload()


@pytest.mark.parametrize("kind", ["global", "port"])
async def test_failed_poe_write_preserves_original_error(hass, switch_server, kind):
    coordinator, _ = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_update_listeners = Mock()
    if kind == "global":
        coordinator._api.set_poe_limit = AsyncMock(
            side_effect=ValueError("write failed")
        )
        coordinator._api.get_poe_state = AsyncMock()
        getter = coordinator._api.get_poe_state
        action, args = coordinator.async_set_poe_limit, (40,)
    else:
        coordinator._api.set_port_poe_settings = AsyncMock(
            side_effect=ValueError("write failed")
        )
        coordinator._api.get_port_poe_states = AsyncMock()
        getter = coordinator._api.get_port_poe_states
        action, args = (
            coordinator.async_set_port_poe_settings,
            (1, False, PoePriority.MIDDLE, PoePowerLimit.AUTO),
        )
    with pytest.raises(ValueError, match="write failed"):
        await action(*args)
    getter.assert_not_awaited()
    coordinator.async_update_listeners.assert_not_called()
    await coordinator.async_unload()


@pytest.mark.parametrize("kind", ["global", "port"])
async def test_poe_action_recovers_after_readback_failure(hass, switch_server, kind):
    api, state, _ = switch_server
    coordinator, _ = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    action = (
        coordinator.async_set_poe_limit
        if kind == "global"
        else coordinator.async_set_port_poe_settings
    )
    args = (
        (40,)
        if kind == "global"
        else (1, False, PoePriority.MIDDLE, PoePowerLimit.AUTO)
    )
    # Fail reads only after a real successful write (preflight reads still work).
    original_post = api._core_api.post

    async def write_then_fail_readback(path, data=None, **kwargs):
        response = await original_post(path, data, **kwargs)
        state["status"]["PoeConfigRpm.htm"] = 500
        return response

    api._core_api.post = write_then_fail_readback
    with pytest.raises(HomeAssistantError, match="command succeeded"):
        await action(*args)
    state["status"].clear()
    await coordinator.async_refresh()
    assert coordinator.get_poe_state() is not None
    assert coordinator.ports_poe_count == 2
    await coordinator.async_unload()


async def test_probe_for_one_feature_invalidates_both_lost_poe_caches(
    hass, switch_server
):
    api, state, _ = switch_server
    state["status"]["PortStatisticsRpm.htm"] = 500
    coordinator, _ = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    assert coordinator.get_poe_state() is not None
    assert coordinator.ports_poe_count == 2
    state["status"] = {"PoeConfigRpm.htm": 404}
    api._features_retry_at = 0
    from custom_components.tplink_easy_smart.client.const import FEATURE_STATS

    assert await coordinator.is_feature_available(FEATURE_STATS)
    assert coordinator.get_poe_state() is None
    assert coordinator.ports_poe_count == 0
    await coordinator.async_unload()
