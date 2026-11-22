from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType
from custom_components.tplink_easy_smart.config_flow import (
    TpLinkControllerConfigFlow,
    TpLinkControllerOptionsFlowHandler,
)
from custom_components.tplink_easy_smart.client.coreapi import AuthenticationError
from custom_components.tplink_easy_smart.const import CONF_SWITCH_MAC, DOMAIN
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
    client.get_device_info = AsyncMock(
        return_value=SimpleNamespace(mac="aa:bb:cc:dd:ee:ff")
    )
    with patch(
        "custom_components.tplink_easy_smart.config_flow.TpLinkApi",
        return_value=client,
    ):
        result = await flow.async_step_user(dict(data))
        assert result["type"] == FlowResultType.CREATE_ENTRY
        client.disconnect.assert_awaited_once()
    entry = make_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    with patch("custom_components.tplink_easy_smart.config_flow.TpLinkApi") as api:
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
    client.get_device_info = AsyncMock(
        return_value=SimpleNamespace(mac="aa:bb:cc:dd:ee:ff")
    )
    with patch(
        "custom_components.tplink_easy_smart.config_flow.TpLinkApi",
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


@pytest.mark.parametrize(
    "scenario,error",
    [
        ("same_switch", None),
        ("legacy_entry", None),
        ("different_switch", "wrong_switch"),
        ("duplicate_address", "address_in_use"),
        ("duplicate_mac", "address_in_use"),
        ("unreachable", "cannot_connect"),
        ("bad_credentials", "auth_invalid_credentials"),
    ],
)
async def test_reconfigure_switch_address(hass, scenario, error):
    mac = "aa:bb:cc:dd:ee:ff"
    data = dict(make_entry().data)
    if scenario != "legacy_entry":
        data[CONF_SWITCH_MAC] = mac
    entry = make_entry(
        data=data, unique_id="preserved-id", options={"scan_interval": 15}
    )
    hass.config_entries._entries[entry.entry_id] = entry
    new_host = "192.168.1.40"
    if scenario in {"duplicate_address", "duplicate_mac"}:
        other_data = dict(make_entry().data)
        if scenario == "duplicate_address":
            other_data["host"] = new_host
        else:
            del data[CONF_SWITCH_MAC]
            other_data[CONF_SWITCH_MAC] = mac
        other = make_entry(data=other_data)
        hass.config_entries._entries[other.entry_id] = other

    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.handler = DOMAIN
    result = await flow.async_step_reconfigure()
    assert result["step_id"] == "reconfigure"
    assert result["data_schema"]({})["host"] == "127.0.0.1"

    if scenario == "different_switch":
        mac = "aa:bb:cc:dd:ee:12"
    client = Mock(
        get_device_info=AsyncMock(return_value=SimpleNamespace(mac=mac)),
        disconnect=AsyncMock(),
    )
    if scenario == "unreachable":
        client.get_device_info.side_effect = ValueError("No switch information")
    elif scenario == "bad_credentials":
        client.get_device_info.side_effect = AuthenticationError(
            "bad", "auth_invalid_credentials"
        )
    with (
        patch(
            "custom_components.tplink_easy_smart.config_flow.TpLinkApi",
            return_value=client,
        ) as api,
        patch.object(
            hass.config_entries, "async_reload", AsyncMock(return_value=True)
        ) as reload_entry,
    ):
        result = await flow.async_step_reconfigure({"host": new_host})
        await hass.async_block_till_done()

    assert api.call_args.kwargs["host"] == new_host
    assert api.call_args.kwargs["password"] == data["password"]
    client.disconnect.assert_awaited_once()
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    assert entry.unique_id == "preserved-id"
    assert entry.options["scan_interval"] == 15
    if error:
        assert result["errors"] == {"base": error}
        assert dict(entry.data) == data
        reload_entry.assert_not_awaited()
    else:
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert dict(entry.data) == {**data, "host": new_host, CONF_SWITCH_MAC: mac}
        reload_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("new_password", [None, "", "replacement-secret"])
async def test_reconfigure_all_setup_fields(hass, new_password):
    mac = "aa:bb:cc:dd:ee:ff"
    entry = make_entry(
        data={**make_entry().data, CONF_SWITCH_MAC: mac},
        unique_id="stable-id",
        options={"scan_interval": 45},
    )
    hass.config_entries._entries[entry.entry_id] = entry
    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.handler = DOMAIN
    result = await flow.async_step_reconfigure()
    schema = result["data_schema"]
    setup_fields = {"name", "host", "port", "username", "password", "ssl", "verify_ssl"}
    assert {str(key) for key in schema.schema} == setup_fields
    assert schema({}) == {key: entry.data[key] for key in setup_fields - {"password"}}

    changes = {
        "name": "Renamed Switch",
        "host": "switch.example",
        "port": 8443,
        "username": "operator",
        "ssl": True,
        "verify_ssl": True,
    }
    if new_password is not None:
        changes["password"] = new_password
    password = new_password or entry.data["password"]
    client = Mock(
        get_device_info=AsyncMock(return_value=SimpleNamespace(mac=mac)),
        disconnect=AsyncMock(),
    )
    with (
        patch(
            "custom_components.tplink_easy_smart.config_flow.TpLinkApi",
            return_value=client,
        ) as api,
        patch.object(
            hass.config_entries, "async_reload", AsyncMock(return_value=True)
        ) as reload_entry,
    ):
        client.get_device_info.side_effect = AuthenticationError(
            "bad", "auth_invalid_credentials"
        )
        result = await flow.async_step_reconfigure(schema(changes))
        assert result["errors"] == {"base": "auth_invalid_credentials"}
        assert "password" not in result["data_schema"]({})
        assert result["data_schema"]({})["host"] == changes["host"]
        assert entry.title == "Test Switch"
        assert entry.data["host"] == "127.0.0.1"
        reload_entry.assert_not_awaited()

        client.get_device_info.side_effect = None
        result = await flow.async_step_reconfigure(schema(changes))
        await hass.async_block_till_done()

    api.assert_called_with(
        host="switch.example", port=8443, use_ssl=True,
        user="operator", password=password, verify_ssl=True,
    )
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == {**changes, "password": password, CONF_SWITCH_MAC: mac}
    assert entry.title == "Renamed Switch"
    assert entry.unique_id == "stable-id"
    assert entry.options["scan_interval"] == 45
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    reload_entry.assert_awaited_once_with(entry.entry_id)


async def test_reconfigure_rejects_duplicate_name(hass):
    entry = make_entry()
    other = make_entry(data={**entry.data, "name": "Other"})
    for configured in (entry, other):
        hass.config_entries._entries[configured.entry_id] = configured
    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    flow.handler = DOMAIN
    with patch("custom_components.tplink_easy_smart.config_flow.TpLinkApi") as api:
        result = await flow.async_step_reconfigure({"name": "Other"})
    assert result["errors"] == {"base": "name_exists"}
    assert entry.data["name"] == "Test"
    api.assert_not_called()
