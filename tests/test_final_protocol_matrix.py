"""Systematic coverage of supported control values and transport failure classes."""

from unittest.mock import AsyncMock
import json

import pytest
from custom_components.tplink_easy_smart.client.classes import (
    PoePowerLimit,
    PoePriority,
    PoePowerStatus,
)
from custom_components.tplink_easy_smart.client.coreapi import (
    ApiCallError,
    _get_variables,
    _convert_value,
    VariableType,
)
from custom_components.tplink_easy_smart.client.tplink_api import ActionError


@pytest.mark.parametrize("priority", list(PoePriority))
@pytest.mark.parametrize("limit", list(PoePowerLimit) + [0.1, 10, 30.0])
async def test_supported_poe_controls_round_trip(switch_server, priority, limit):
    api, state, _ = switch_server
    await api.set_port_poe_settings(1, True, priority, limit)
    ports = await api.get_port_poe_states()
    assert ports[0].enabled
    assert ports[0].priority == priority
    if isinstance(limit, PoePowerLimit):
        assert ports[0].power_limit == limit
    else:
        # The read endpoint reports wattage, not the class/manual mode. A
        # manual 30 W limit has the same encoding as the Class 4 preset.
        actual = ports[0].power_limit
        watts = actual.value / 10 if isinstance(actual, PoePowerLimit) else actual
        assert watts == limit
    assert state["writes"][-1]["name_ppriority"] == str(int(priority) + 1)


@pytest.mark.parametrize("status", list(PoePowerStatus))
async def test_all_documented_poe_status_values_parse(switch_server, status):
    api, state, _ = switch_server
    state["poe_status"][0] = int(status)
    assert (await api.get_port_poe_states())[0].power_status == status


@pytest.mark.parametrize("limit", [0, 0.9, 65.1, 1000])
async def test_global_limit_respects_actual_device_range(switch_server, limit):
    api, state, _ = switch_server
    with pytest.raises(ActionError):
        await api.set_poe_limit(limit)
    assert not state["writes"]


@pytest.mark.parametrize("status", [401, 403])
async def test_persistent_unauthorized_response_only_reauthenticates_once(
    switch_server, status
):
    api, state, _ = switch_server
    await api.get_device_info()
    state["status"]["SystemInfoRpm.htm"] = status
    with pytest.raises(ApiCallError) as ex:
        await api.get_device_info()
    assert ex.value.category == "unauthorized"
    assert state["logins"] == 2
    assert not api._core_api._is_initialized


async def test_uint64_packet_counters_survive_real_http_parse(switch_server):
    api, state, _ = switch_server
    value = 2**64 - 1
    state["packets"] = [value] * 8
    packets = await api.get_port_statistics()
    assert packets[0].tx_good_pkts == value
    assert packets[1].rx_bad_pkts == value


@pytest.mark.parametrize(
    "value",
    [
        "a;b",
        "x,y",
        "http://host/var n=0;",
        "/*comment*/",
        "quote\" and 'single'",
        "backslash\\end",
        "line\nbreak",
    ],
)
def test_literal_strings_preserve_content_and_hide_fake_declarations(value):
    encoded = json.dumps(value)
    script = f'<script>var info={{"name":{encoded},"values":[1,2]}};var n=2;/* var n=0; */</script>'
    values = _get_variables(script)
    parsed = _convert_value(values["info"], VariableType.Dict)
    assert parsed["name"] == value
    assert _convert_value(values["n"], VariableType.Int) == 2


@pytest.mark.parametrize("operation", ["poe_limit", "poe_port", "clear"])
async def test_unsupported_feature_actions_never_write(switch_server, operation):
    api, state, _ = switch_server
    api.is_feature_available = AsyncMock(return_value=False)
    if operation == "poe_limit":
        call = lambda: api.set_poe_limit(40)
    elif operation == "poe_port":
        call = lambda: api.set_port_poe_settings(
            1, True, PoePriority.HIGH, PoePowerLimit.AUTO
        )
    else:
        call = api.clear_port_statistics
    with pytest.raises(ActionError):
        await call()
    assert state["clears"] == 0 and not state["writes"]


@pytest.mark.parametrize("value", [42, True, {}, [], None])
async def test_optional_device_metadata_has_safe_types(switch_server, value):
    api, _, _ = switch_server
    api._core_api.get_variable = AsyncMock(
        return_value={
            "macStr": ["aa:bb:cc:dd:ee:ff"],
            "descriStr": [value],
            "firmwareStr": [value],
            "hardwareStr": [value],
            "ipStr": [value],
        }
    )
    info = await api.get_device_info()
    assert info.name == "TP-Link Switch"
    assert info.firmware is None and info.hardware is None and info.ip is None
