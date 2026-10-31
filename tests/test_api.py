"""Invalid switch data, capability recovery and control validation."""

import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from custom_components.tplink_easy_smart.client import tplink_api
from custom_components.tplink_easy_smart.client.const import FEATURE_POE, FEATURE_STATS
from custom_components.tplink_easy_smart.client.coreapi import ApiCallError
from custom_components.tplink_easy_smart.client.classes import PortSpeed, PoePriority
from custom_components.tplink_easy_smart import binary_sensor, button, sensor, switch
from test_homeassistant import coordinator_for


@pytest.mark.parametrize("field", ["state", "spd_cfg", "spd_act", "fc_cfg", "fc_act"])
@pytest.mark.parametrize("value", [None, [], [1]])
async def test_incomplete_port_arrays_are_rejected(switch_server, field, value):
    api, _, _ = switch_server
    data = {
        "state": [1, 1],
        "spd_cfg": [1, 1],
        "spd_act": [6, 0],
        "fc_cfg": [0, 0],
        "fc_act": [0, 0],
    }
    api._core_api.get_variables = AsyncMock(
        return_value={"all_info": {**data, field: value}, "max_port_num": 2}
    )
    with pytest.raises(ValueError):
        await api.get_port_states()


@pytest.mark.parametrize(
    "field",
    [
        "state",
        "priority",
        "powerlimit",
        "power",
        "current",
        "voltage",
        "pdclass",
        "powerstatus",
    ],
)
async def test_incomplete_poe_arrays_are_rejected(switch_server, field):
    api, _, _ = switch_server
    api.is_feature_available = AsyncMock(return_value=True)
    data = {
        "state": [1],
        "priority": [0],
        "powerlimit": [330],
        "power": [10],
        "current": [20],
        "voltage": [500],
        "pdclass": [40],
        "powerstatus": [2],
    }
    api._core_api.get_variables = AsyncMock(
        return_value={"portConfig": {**data, field: []}, "poe_port_num": 1}
    )
    with pytest.raises(ValueError):
        await api.get_port_poe_states()


@pytest.mark.parametrize("count", [0, -1, None, True, "bad"])
async def test_invalid_port_count(switch_server, count):
    api, _, _ = switch_server
    api._core_api.get_variables = AsyncMock(
        return_value={"all_info": {}, "max_port_num": count}
    )
    with pytest.raises(ValueError):
        await api.get_port_states()


@pytest.mark.parametrize(
    "info", [None, {}, {"macStr": []}, {"macStr": ["not-a-mac"]}, {"macStr": None}]
)
async def test_invalid_identity_is_rejected_before_entity_setup(switch_server, info):
    api, _, _ = switch_server
    api._core_api.get_variable = AsyncMock(return_value=info)
    with pytest.raises(ValueError):
        await api.get_device_info()


async def test_incomplete_statistics_are_not_reported_as_success(switch_server):
    api, state, _ = switch_server
    await api.get_device_info()
    state["packets"] = [1, 2, 3]
    with pytest.raises(ValueError):
        await api.get_port_statistics()


@pytest.mark.parametrize("number", [0, -1, True, 3])
async def test_invalid_port_control_does_not_send_command(switch_server, number):
    api, _, _ = switch_server
    with pytest.raises((ValueError, tplink_api.ActionError)):
        await api.set_port_state(number, True, PortSpeed.AUTO, False)


@pytest.mark.parametrize("limit", [float("nan"), float("inf"), -1, True])
async def test_nonfinite_poe_limits_are_rejected(switch_server, limit):
    api, state, _ = switch_server
    with pytest.raises(ValueError):
        await api.set_poe_limit(limit)
    assert state["writes"] == []


async def test_poe_controls_accept_manual_integer_and_float_limits(switch_server):
    api, state, _ = switch_server
    await api.set_port_poe_settings(1, False, PoePriority.MIDDLE, 10)
    await api.set_poe_limit(40)
    assert len(state["writes"]) == 2
    assert state["writes"][0]["name_pstate"] == "1"


async def test_recovered_capabilities_add_entities_without_duplicates(
    hass, switch_server
):
    api, state, _ = switch_server
    state["status"].update({"PoeConfigRpm.htm": 500, "PortStatisticsRpm.htm": 500})
    coordinator, entry = coordinator_for(
        hass, switch_server, port_state_switches=True, poe_state_switches=True
    )
    await coordinator.async_config_entry_first_refresh()
    assert not coordinator.is_feature_available_cached(FEATURE_POE)
    entities = []
    for module in (binary_sensor, sensor, switch, button):
        await module.async_setup_entry(hass, entry, entities.extend)
    assert len(entities) == 5  # two links, network info and two port switches
    state["status"].clear()
    api._features_retry_at = 0
    await coordinator.async_refresh()
    assert coordinator.is_feature_available_cached(FEATURE_POE)
    assert coordinator.is_feature_available_cached(FEATURE_STATS)
    assert len(entities) == 11
    await coordinator.async_refresh()
    assert len(entities) == 11
    assert len(set(e.unique_id for e in entities)) == 11
    await entry._async_process_on_unload(hass)
    await coordinator.async_unload()


async def test_unsupported_feature_does_not_block_setup_and_is_rechecked(switch_server):
    api, state, _ = switch_server
    state["status"]["PoeConfigRpm.htm"] = 404
    assert not await api.is_feature_available(FEATURE_POE)
    assert await api.is_feature_available(FEATURE_STATS)
    assert state["logins"] == 1
    assert not api._is_features_updated  # Negative probes expire too.
    state["status"].clear()
    api._features_retry_at = 0
    assert await api.is_feature_available(FEATURE_POE)


async def test_feature_cooldown_and_concurrent_requests(switch_server):
    api, _, _ = switch_server
    api._features.update = AsyncMock(return_value=False)
    await asyncio.gather(*(api.is_feature_available(FEATURE_POE) for _ in range(10)))
    api._features.update.assert_awaited_once()
