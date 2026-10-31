"""Test production integration objects against Home Assistant 2026.10.4."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from custom_components import tplink_easy_smart as integration
from custom_components.tplink_easy_smart import binary_sensor, button, sensor, switch
from custom_components.tplink_easy_smart.client.classes import (
    PortState,
    PortSpeed,
    PortPoeState,
    PoePriority,
    PoePowerLimit,
    PoeClass,
    PoePowerStatus,
)
from custom_components.tplink_easy_smart.client.coreapi import AuthenticationError
from custom_components.tplink_easy_smart.const import DOMAIN, DATA_KEY_COORDINATOR
from custom_components.tplink_easy_smart.displayed_values import DISPLAYED_POE_CLASSES
from custom_components.tplink_easy_smart.update_coordinator import (
    TpLinkDataUpdateCoordinator,
)
from conftest import make_entry


def coordinator_for(hass, server, **options):
    api, state, port = server
    entry = make_entry(options=options)
    hass.config_entries._entries[entry.entry_id] = entry
    coordinator = TpLinkDataUpdateCoordinator(hass, entry)
    coordinator._api = api
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_KEY_COORDINATOR: coordinator
    }
    return coordinator, entry


async def test_real_coordinator_refresh(hass, switch_server):
    coordinator, entry = coordinator_for(hass, switch_server)
    assert coordinator.config_entry is entry
    await coordinator.async_config_entry_first_refresh()
    assert coordinator.last_update_success and coordinator.ports_count == 2
    assert coordinator.ports_poe_count == 2
    assert coordinator.get_device_info()["identifiers"] == {
        (DOMAIN, "aa:bb:cc:dd:ee:ff")
    }
    await coordinator.async_unload()


async def test_all_real_entity_platforms_and_availability(hass, switch_server):
    coordinator, entry = coordinator_for(
        hass, switch_server, port_state_switches=True, poe_state_switches=True
    )
    await coordinator.async_config_entry_first_refresh()
    entities = []
    for module in (binary_sensor, sensor, switch, button):
        await module.async_setup_entry(hass, entry, entities.extend)
    assert len(entities) == 11  # 4 connectivity/power + 2 sensors + 4 controls + button
    ids = [e.unique_id for e in entities]
    assert len(set(ids)) == len(ids)
    for entity in entities:
        entity.hass = hass
        entity.async_write_ha_state = Mock()
        if hasattr(entity, "_port_number") or isinstance(entity, sensor.TpLinkSensor):
            entity._handle_coordinator_update()
    port = next(
        e
        for e in entities
        if isinstance(e, binary_sensor.TpLinkPortStateBinarySensor)
        and e._port_number == 2
    )
    assert port.available and port.is_on is False
    poe = next(
        e
        for e in entities
        if isinstance(e, binary_sensor.TpLinkPortPoeStateBinarySensor)
        and e._port_number == 2
    )
    assert poe.available and poe.is_on is False
    coordinator.last_update_success = False
    assert all(not e.available for e in entities)
    coordinator.last_update_success = True
    # Stale counters disappear without changing known connectivity.
    coordinator._port_statistics = []
    port._handle_coordinator_update()
    assert not any(k.endswith("_packets") for k in port.extra_state_attributes)
    await entry._async_process_on_unload(hass)
    await coordinator.async_unload()


@pytest.mark.parametrize(
    "status",
    [
        PoePowerStatus.OFF,
        PoePowerStatus.TURNING_ON,
        PoePowerStatus.SHORT,
        PoePowerStatus.OVERTEMPERATURE,
        PoePowerStatus.ON,
    ],
)
async def test_poe_faults_do_not_report_power_on(hass, switch_server, status):
    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    coordinator._port_poe_states[0].power_status = status
    entities = []
    await binary_sensor.async_setup_entry(hass, entry, entities.extend)
    entity = next(
        e
        for e in entities
        if isinstance(e, binary_sensor.TpLinkPortPoeStateBinarySensor)
        and e._port_number == 1
    )
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entity._handle_coordinator_update()
    assert entity.is_on == (status == PoePowerStatus.ON)
    await entry._async_process_on_unload(hass)
    await coordinator.async_unload()


async def test_reset_refresh_failure_and_recovery(hass, switch_server):
    coordinator, entry = coordinator_for(hass, switch_server)
    await coordinator.async_config_entry_first_refresh()
    api, state, _ = switch_server
    state["status"]["PortStatisticsRpm.htm"] = 500
    with pytest.raises(HomeAssistantError, match="were cleared"):
        await coordinator.async_clear_port_statistics()
    assert state["clears"] == 1 and coordinator._port_statistics == []
    state["status"].clear()
    await coordinator.async_update()
    assert coordinator.get_port_statistics(1).tx_good_pkts == 0
    state["status"]["port_statistics_set.cgi"] = 500
    with pytest.raises(Exception) as ex:
        await coordinator.async_clear_port_statistics()
    assert "were cleared" not in str(ex.value)
    await coordinator.async_unload()


async def test_migration_uses_real_immutable_config_entry(hass):
    entry = make_entry(version=1, options={"port_state_switches": True})
    hass.config_entries._entries[entry.entry_id] = entry
    assert await integration.async_migrate_entry(hass, entry)
    assert entry.version == 2 and entry.options["port_state_switches"]
    assert entry.options["poe_state_switches"] is False
    future = make_entry(version=3)
    assert await integration.async_migrate_entry(hass, future) is False


@pytest.mark.parametrize("success", [True, False])
async def test_unload_preserves_services_until_success(hass, success):
    entry = make_entry()
    obj = Mock(spec=TpLinkDataUpdateCoordinator)
    obj.async_unload = AsyncMock()
    hass.data[DOMAIN] = {entry.entry_id: {DATA_KEY_COORDINATOR: obj}}
    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=success),
        ),
        patch.object(integration, "async_unload_services", AsyncMock()) as unload,
    ):
        assert await integration.async_unload_entry(hass, entry) == success
        assert unload.await_count == int(success)
        assert obj.async_unload.await_count == int(success)
        assert (DATA_KEY_COORDINATOR in hass.data[DOMAIN][entry.entry_id]) == (
            not success
        )


@pytest.mark.parametrize("failure_stage", ["refresh", "forward"])
async def test_failed_setup_cleanup(hass, failure_stage):
    entry = make_entry()
    obj = Mock(spec=TpLinkDataUpdateCoordinator)
    obj.async_config_entry_first_refresh = AsyncMock(
        side_effect=RuntimeError("failed") if failure_stage == "refresh" else None
    )
    obj.async_unload = AsyncMock()
    with (
        patch.object(integration, "TpLinkDataUpdateCoordinator", return_value=obj),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=RuntimeError("failed")),
        ),
    ):
        with pytest.raises(RuntimeError):
            await integration.async_setup_entry(hass, entry)
    obj.async_unload.assert_awaited_once()
    assert DATA_KEY_COORDINATOR not in hass.data[DOMAIN][entry.entry_id]
    await entry._async_process_on_unload(hass)


@pytest.mark.parametrize("interval", [0, -1, None, True, "bad", float("nan")])
async def test_corrupt_interval_falls_back(hass, interval):
    entry = make_entry(options={"scan_interval": interval})
    obj = TpLinkDataUpdateCoordinator(hass, entry)
    assert obj.update_interval.total_seconds() == 30
    await obj.async_unload()


async def test_bad_credentials_signal_reauth(hass, switch_server):
    coordinator, _ = coordinator_for(hass, switch_server)
    coordinator._api.get_device_info = AsyncMock(
        side_effect=AuthenticationError("wrong password", "auth_invalid_credentials")
    )
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_update()
    await coordinator.async_unload()


async def test_initial_missing_ports_is_retryable(hass, switch_server):
    coordinator, _ = coordinator_for(hass, switch_server)
    coordinator._api.get_port_states = AsyncMock(return_value=[])
    with pytest.raises(UpdateFailed):
        await coordinator.async_update()
    await coordinator.async_unload()


def test_poe_class_labels():
    for num in range(5):
        assert (
            DISPLAYED_POE_CLASSES[getattr(PoeClass, f"CLASS_{num}")] == f"Class {num}"
        )
