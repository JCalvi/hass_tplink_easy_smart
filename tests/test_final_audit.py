"""Final lifecycle, bounds and protocol edge-case audit."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components import tplink_easy_smart as integration
from custom_components.tplink_easy_smart.client.coreapi import (
    _get_variables,
    _convert_value,
    VariableType,
)
from custom_components.tplink_easy_smart.client.tplink_api import _integer
from custom_components.tplink_easy_smart.config_flow import (
    _positive_interval,
    _IntervalSelector,
)
from custom_components.tplink_easy_smart.const import (
    DEFAULT_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
)
from custom_components.tplink_easy_smart.update_coordinator import (
    TpLinkDataUpdateCoordinator,
)
from test_homeassistant import coordinator_for
from conftest import make_entry


@pytest.mark.parametrize(
    "value", [86401, 10**14, 10**100, "86401", "9" * 10000, True, 1.5, 0, -1]
)
def test_interval_upper_bound_rejects_unusable_values(value):
    with pytest.raises(vol.Invalid):
        _positive_interval(value)


@pytest.mark.parametrize("value", [1, 86400, "86400", "00086400", "0" * 10000 + "1"])
def test_interval_bounds_accept_whole_seconds(value):
    assert 1 <= _positive_interval(value) <= MAX_SCAN_INTERVAL


def test_interval_selector_exposes_same_maximum_to_ui():
    assert (
        _IntervalSelector().serialize()["selector"]["number"]["max"]
        == MAX_SCAN_INTERVAL
    )


@pytest.mark.parametrize("value", [86401, 10**14, 10**100, "9" * 10000])
async def test_stored_invalid_interval_falls_back_without_setup_overflow(hass, value):
    entry = make_entry(options={"scan_interval": value})
    coordinator = TpLinkDataUpdateCoordinator(hass, entry)
    assert coordinator.update_interval.total_seconds() == DEFAULT_SCAN_INTERVAL
    await coordinator.async_unload()
    await entry._async_process_on_unload(hass)


async def test_normal_ha_shutdown_closes_session_and_stops_polling(hass, switch_server):
    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    session = coordinator._api._core_api._session
    assert session is not None and not session.closed
    await hass.async_stop(force=True)
    assert session.closed
    assert coordinator._api._core_api._session is None
    assert coordinator._shutdown_requested
    assert coordinator._unsub_api_stop is None
    logins = switch_server[1]["logins"]
    await coordinator.async_refresh()
    assert switch_server[1]["logins"] == logins
    await entry._async_process_on_unload(hass)


async def test_unload_and_repeated_cleanup_are_idempotent(hass, switch_server):
    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    session = coordinator._api._core_api._session
    await coordinator.async_unload()
    await coordinator.async_unload()
    assert session.closed and coordinator._unsub_api_stop is None
    assert coordinator._shutdown_requested
    await entry._async_process_on_unload(hass)
    await hass.async_stop(force=True)


async def test_failed_setup_removes_shutdown_hook(hass, switch_server):
    coordinator, entry = coordinator_for(hass, switch_server)
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=ConfigEntryNotReady("offline")
    )
    with patch.object(
        integration, "TpLinkDataUpdateCoordinator", return_value=coordinator
    ):
        with pytest.raises(ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)
    assert coordinator._unsub_api_stop is None
    assert coordinator._shutdown_requested
    await entry._async_process_on_unload(hass)


async def test_shutdown_waits_for_inflight_request_then_closes(hass, switch_server):
    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    core = coordinator._api._core_api
    session = core._session
    request_started = asyncio.Event()
    allow_response = asyncio.Event()
    shutdown_started = asyncio.Event()
    original_get = core._get_raw
    original_unload = coordinator.async_unload

    async def blocked_get(path):
        request_started.set()
        await allow_response.wait()
        return await original_get(path)

    async def begin_shutdown():
        shutdown_started.set()
        await original_unload()

    core._get_raw = blocked_get
    coordinator.async_unload = begin_shutdown
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(request_started.wait(), 2)
    stop = asyncio.create_task(hass.async_stop(force=True))
    await asyncio.wait_for(shutdown_started.wait(), 2)
    assert not stop.done()
    allow_response.set()
    await asyncio.wait_for(asyncio.gather(refresh, stop), 5)
    assert session.closed
    assert core._session is None
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize("value", [0, 1, 2**32 - 1, 2**53 + 1, 2**63 - 1, 2**64 - 1])
def test_integer_counters_keep_exact_precision(value):
    assert _integer(value, "packet counter") == value
    assert _integer(str(value), "packet counter") == value


@pytest.mark.parametrize(
    "script",
    [
        "var n=2; // var n=0;",
        "var n=2; /* var n=0; */",
        "/* var n=0; */ var n=2;",
        'const debug="var n=0;"; var n=2;',
        'var n=2; const debug="var n=0;";',
        "var n=2 /* comment ; with } bracket */;",
    ],
)
def test_javascript_comments_and_debug_strings_do_not_override_values(script):
    result = _get_variables("<script>" + script + "</script>")
    assert _convert_value(result.get("n"), VariableType.Int) == 2


@pytest.mark.parametrize("action", ["port", "clear", "global_poe", "port_poe"])
async def test_actions_after_shutdown_never_reopen_session(hass, switch_server, action):
    from custom_components.tplink_easy_smart.client.classes import (
        PortSpeed,
        PoePriority,
        PoePowerLimit,
    )
    from homeassistant.exceptions import HomeAssistantError

    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_unload()
    logins = switch_server[1]["logins"]
    calls = {
        "port": lambda: coordinator.set_port_state(1, True, PortSpeed.AUTO, False),
        "clear": coordinator.async_clear_port_statistics,
        "global_poe": lambda: coordinator.async_set_poe_limit(40),
        "port_poe": lambda: coordinator.async_set_port_poe_settings(
            1, True, PoePriority.MIDDLE, PoePowerLimit.AUTO
        ),
    }
    with pytest.raises(HomeAssistantError, match="stopping"):
        await calls[action]()
    assert coordinator._api._core_api._session is None
    assert switch_server[1]["logins"] == logins
    await entry._async_process_on_unload(hass)


async def test_queued_action_is_rejected_when_shutdown_starts(hass, switch_server):
    from homeassistant.exceptions import HomeAssistantError

    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    stop_started = asyncio.Event()
    original = coordinator.async_shutdown

    async def shutdown():
        await original()
        stop_started.set()

    coordinator.async_shutdown = shutdown
    await coordinator._operation_lock.acquire()
    action = asyncio.create_task(coordinator.async_set_poe_limit(40))
    await asyncio.sleep(0)
    stop = asyncio.create_task(coordinator.async_unload())
    await asyncio.wait_for(stop_started.wait(), 2)
    coordinator._operation_lock.release()
    with pytest.raises(HomeAssistantError, match="stopping"):
        await action
    await asyncio.wait_for(stop, 2)
    assert not switch_server[1]["writes"]
    assert coordinator._api._core_api._session is None
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize("kind", ["clear", "global_poe", "port_poe"])
async def test_cancelled_readback_invalidates_cached_values(hass, switch_server, kind):
    from unittest.mock import Mock
    from custom_components.tplink_easy_smart.client.classes import (
        PoePriority,
        PoePowerLimit,
    )

    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_update_listeners = Mock()
    if kind == "clear":
        coordinator._api.clear_port_statistics = AsyncMock()
        coordinator._api.get_port_statistics = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        call = coordinator.async_clear_port_statistics
        cache, empty = "_port_statistics", []
    elif kind == "global_poe":
        coordinator._api.set_poe_limit = AsyncMock()
        coordinator._api.get_poe_state = AsyncMock(side_effect=asyncio.CancelledError())
        call = lambda: coordinator.async_set_poe_limit(40)
        cache, empty = "_poe_state", None
    else:
        coordinator._api.set_port_poe_settings = AsyncMock()
        coordinator._api.get_port_poe_states = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        call = lambda: coordinator.async_set_port_poe_settings(
            1, True, PoePriority.MIDDLE, PoePowerLimit.AUTO
        )
        cache, empty = "_port_poe_states", []
    with pytest.raises(asyncio.CancelledError):
        await call()
    assert getattr(coordinator, cache) == empty
    coordinator.async_update_listeners.assert_called_once()
    assert not coordinator._operation_lock.locked()
    await coordinator.async_unload()
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize("kind", ["port", "clear", "global_poe", "port_poe"])
@pytest.mark.parametrize("error_type", ["cancel", "transport"])
async def test_uncertain_write_invalidates_cache_and_preserves_error(
    hass, switch_server, kind, error_type
):
    from unittest.mock import Mock
    from custom_components.tplink_easy_smart.client.coreapi import ApiCallError
    from custom_components.tplink_easy_smart.client.classes import (
        PortSpeed,
        PoePriority,
        PoePowerLimit,
    )

    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_update_listeners = Mock()
    error = (
        asyncio.CancelledError()
        if error_type == "cancel"
        else ApiCallError("lost response", -3, "request_error")
    )
    if kind == "port":
        write, cache, empty = "set_port_state", "_port_states", []
        call = lambda: coordinator.set_port_state(1, True, PortSpeed.AUTO, False)
    elif kind == "clear":
        write, cache, empty = "clear_port_statistics", "_port_statistics", []
        call = coordinator.async_clear_port_statistics
    elif kind == "global_poe":
        write, cache, empty = "set_poe_limit", "_poe_state", None
        call = lambda: coordinator.async_set_poe_limit(40)
    else:
        write, cache, empty = "set_port_poe_settings", "_port_poe_states", []
        call = lambda: coordinator.async_set_port_poe_settings(
            1, True, PoePriority.MIDDLE, PoePowerLimit.AUTO
        )
    setattr(coordinator._api, write, AsyncMock(side_effect=error))
    with pytest.raises(type(error)) as result:
        await call()
    assert result.value is error
    assert getattr(coordinator, cache) == empty
    coordinator.async_update_listeners.assert_called_once()
    await coordinator.async_unload()
    await entry._async_process_on_unload(hass)


async def test_action_preflight_capability_loss_invalidates_all_poe_readings(
    hass, switch_server
):
    from custom_components.tplink_easy_smart.client.tplink_api import ActionError

    api, state, _ = switch_server
    state["status"]["PortStatisticsRpm.htm"] = 500
    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    assert coordinator.get_poe_state() is not None and coordinator.ports_poe_count == 2
    state["status"] = {"PoeConfigRpm.htm": 404}
    api._features_retry_at = 0
    with pytest.raises(ActionError):
        await coordinator.async_set_poe_limit(40)
    assert coordinator.get_poe_state() is None
    assert coordinator.ports_poe_count == 0
    await coordinator.async_unload()
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize("value", [10**14, "9" * 10000, -1, True])
async def test_options_form_recovers_invalid_saved_interval(hass, value):
    import probatio
    from homeassistant.helpers import config_validation as cv
    from custom_components.tplink_easy_smart.config_flow import (
        TpLinkControllerOptionsFlowHandler,
    )

    entry = make_entry(options={"scan_interval": value})
    hass.config_entries._entries[entry.entry_id] = entry
    flow = TpLinkControllerOptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.context = {"source": "init"}
    result = await flow.async_step_init()
    assert result["data_schema"]({})["scan_interval"] == DEFAULT_SCAN_INTERVAL
    fields = probatio.to_field_list(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert fields[0]["default"] == DEFAULT_SCAN_INTERVAL


@pytest.mark.parametrize("stage", ["refresh", "forward"])
async def test_cancelled_setup_closes_session_and_removes_hook(
    hass, switch_server, stage
):
    coordinator, entry = coordinator_for(hass, switch_server)
    if stage == "refresh":
        coordinator.async_config_entry_first_refresh = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
    with (
        patch.object(
            integration, "TpLinkDataUpdateCoordinator", return_value=coordinator
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await integration.async_setup_entry(hass, entry)
    assert coordinator._api._core_api._session is None
    assert coordinator._unsub_api_stop is None and coordinator._shutdown_requested
    assert not coordinator._operation_lock.locked()
    await entry._async_process_on_unload(hass)
