"""Load and unload through Home Assistant's real integration/platform machinery."""

import shutil
from pathlib import Path
import pytest
from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from custom_components.tplink_easy_smart.const import DOMAIN
from conftest import make_entry


async def test_full_homeassistant_setup_actions_and_unload(hass, switch_server):
    _, state, port = switch_server
    source = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
    target = Path(hass.config.path("custom_components")) / DOMAIN
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target)
    loader.async_setup(hass)
    hass.config.skip_pip = True
    assert (
        await async_from_config_dict(
            {
                "homeassistant": {
                    "name": "Integration test",
                    "latitude": 0,
                    "longitude": 0,
                    "elevation": 0,
                    "unit_system": "metric",
                    "time_zone": "UTC",
                }
            },
            hass,
        )
        is hass
    )
    entry = make_entry(
        state=ConfigEntryState.NOT_LOADED,
        options={"port_state_switches": True, "poe_state_switches": True},
        data={**make_entry().data, "port": port},
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 11
    button = next(e for e in entities if e.domain == "button")
    await hass.services.async_call(
        "button", "press", {"entity_id": button.entity_id}, blocking=True
    )
    assert state["clears"] == 1
    await hass.services.async_call(
        DOMAIN,
        "set_general_poe_limit",
        {"mac_address": "AA:BB:CC:DD:EE:FF", "power_limit": 40},
        blocking=True,
    )
    assert state["writes"][-1]["name_powerlimit"] == "40.0"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "set_general_poe_limit")
    assert entry.state == ConfigEntryState.NOT_LOADED
