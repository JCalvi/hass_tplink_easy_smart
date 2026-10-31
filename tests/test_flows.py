from unittest.mock import AsyncMock, Mock, patch
import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType
from custom_components.tplink_easy_smart.config_flow import (
    TpLinkControllerConfigFlow,
    TpLinkControllerOptionsFlowHandler,
)
from custom_components.tplink_easy_smart.client.coreapi import AuthenticationError
from custom_components.tplink_easy_smart.const import DOMAIN
from conftest import make_entry


async def test_user_flow_authentication_and_duplicate_name(hass):
    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.handler = DOMAIN
    result = await flow.async_step_user()
    assert result["type"] == FlowResultType.FORM
    data = make_entry().data
    client = Mock()
    client.authenticate = AsyncMock()
    client.disconnect = AsyncMock()
    with patch(
        "custom_components.tplink_easy_smart.config_flow.TpLinkWebApi",
        return_value=client,
    ):
        result = await flow.async_step_user(dict(data))
        assert result["type"] == FlowResultType.CREATE_ENTRY
        client.disconnect.assert_awaited_once()
    entry = make_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    with patch("custom_components.tplink_easy_smart.config_flow.TpLinkWebApi") as api:
        result = await flow.async_step_user(dict(data))
        assert result["errors"]["base"] == "name_exists"
        api.assert_not_called()


async def test_user_bad_credentials_close_client(hass):
    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.handler = DOMAIN
    client = Mock()
    client.authenticate = AsyncMock(
        side_effect=AuthenticationError("bad", "auth_invalid_credentials")
    )
    client.disconnect = AsyncMock()
    with patch(
        "custom_components.tplink_easy_smart.config_flow.TpLinkWebApi",
        return_value=client,
    ):
        result = await flow.async_step_user(dict(make_entry().data))
    assert result["errors"]["base"] == "auth_invalid_credentials"
    client.disconnect.assert_awaited_once()


async def test_options_flow_validates_interval_and_preserves_options(hass):
    entry = make_entry(options={"port_state_switches": True, "existing_option": 123})
    hass.config_entries._entries[entry.entry_id] = entry
    flow = TpLinkControllerOptionsFlowHandler(entry)
    flow.hass = hass
    flow.context = {"source": "init"}
    flow.handler = entry.entry_id
    result = await flow.async_step_init()
    for interval in (0, -1, "bad"):
        with pytest.raises(vol.Invalid):
            result["data_schema"]({"scan_interval": interval})
    assert result["data_schema"]({"scan_interval": 15})["scan_interval"] == 15
    result = await flow.async_step_basic_options({"scan_interval": 15})
    assert result["step_id"] == "features_select"
    result = await flow.async_step_features_select(
        {"port_state_switches": False, "poe_state_switches": True}
    )
    assert (
        result["data"]["existing_option"] == 123
        and result["data"]["scan_interval"] == 15
    )


async def test_reauth_updates_existing_entry_without_recreating_entities(hass):
    entry = make_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.handler = DOMAIN
    result = await flow.async_step_reauth(entry.data)
    assert result["step_id"] == "reauth_confirm"
    client = Mock()
    client.authenticate = AsyncMock()
    client.disconnect = AsyncMock()
    with (
        patch(
            "custom_components.tplink_easy_smart.config_flow.TpLinkWebApi",
            return_value=client,
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        result = await flow.async_step_reauth_confirm(
            {"username": "updated", "password": "new-secret"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.ABORT
    assert (
        entry.data["password"] == "new-secret" and entry.data["username"] == "updated"
    )
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    client.disconnect.assert_awaited_once()
