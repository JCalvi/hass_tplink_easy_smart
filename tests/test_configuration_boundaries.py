"""Strict intervals, duplicate identity and IPv6 regression coverage."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from aiohttp import web
import pytest
import voluptuous as vol
from yarl import URL
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tplink_easy_smart import async_setup_entry, async_unload_entry
from custom_components.tplink_easy_smart.client.coreapi import TpLinkWebApi
from custom_components.tplink_easy_smart.config_flow import (
    TpLinkControllerConfigFlow,
    _positive_interval,
    _switch_host,
)
from custom_components.tplink_easy_smart.const import (
    CONF_SWITCH_MAC,
    DATA_KEY_COORDINATOR,
    DOMAIN,
)
from conftest import make_entry


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.9,
        2.0,
        0,
        -1,
        "1.9",
        "True",
        "bad",
        None,
        float("inf"),
        float("nan"),
    ],
)
def test_interval_rejects_non_integer_inputs(value):
    with pytest.raises(vol.Invalid):
        _positive_interval(value)


@pytest.mark.parametrize(
    "value,expected", [(1, 1), (30, 30), ("30", 30), (" 30 ", 30), ("01", 1)]
)
def test_interval_accepts_whole_seconds(value, expected):
    assert _positive_interval(value) == expected


@pytest.mark.parametrize(
    "host,port,ssl,expected",
    [
        ("::1", 8080, False, "http://[::1]:8080"),
        ("[::1]", 8080, False, "http://[::1]:8080"),
        ("2001:db8::1", 443, True, "https://[2001:db8::1]"),
        ("[2001:db8::1]", 8443, True, "https://[2001:db8::1]:8443"),
        ("fe80::1%eth0", 8080, False, "http://[fe80::1%eth0]:8080"),
        ("192.168.1.2", 80, False, "http://192.168.1.2"),
        ("switch.example", 8080, False, "http://switch.example:8080"),
    ],
)
def test_urls_support_ipv6_and_preserve_ipv4(host, port, ssl, expected):
    api = TpLinkWebApi(host, port, ssl, "admin", "test", False)
    assert api.device_url == expected
    assert URL(api._get_url("test")).path == "/test"


@pytest.mark.parametrize(
    "value", ["", "   ", "http://192.168.1.2", "host:8080", "name/path", "user@host"]
)
def test_host_field_rejects_invalid_host_syntax(value):
    with pytest.raises(vol.Invalid):
        _switch_host(value)


def new_flow(hass):
    flow = TpLinkControllerConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": "user"}
    return flow


@pytest.mark.parametrize(
    "existing_host,new_host",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("Switch.EXAMPLE.", "switch.example"),
        ("[2001:db8::1]", "2001:db8:0:0:0:0:0:1"),
    ],
)
async def test_duplicate_endpoint_rejected_before_login(hass, existing_host, new_host):
    base = make_entry()
    existing = make_entry(data={**base.data, "host": existing_host})
    hass.config_entries._entries[existing.entry_id] = existing
    with patch("custom_components.tplink_easy_smart.config_flow.TpLinkApi") as api:
        result = await new_flow(hass).async_step_user(
            {**base.data, "host": new_host, "name": "Different label"}
        )
    assert (
        result["type"] == FlowResultType.ABORT
        and result["reason"] == "already_configured"
    )
    api.assert_not_called()


@pytest.mark.parametrize("identity_source", ["saved", "live"])
async def test_duplicate_mac_rejected_across_addresses(
    hass, switch_server, identity_source
):
    _, _, port = switch_server
    original = make_entry(
        data={
            **make_entry().data,
            "host": "old-address.example",
            **(
                {CONF_SWITCH_MAC: "AA:BB:CC:DD:EE:FF"}
                if identity_source == "saved"
                else {}
            ),
        }
    )
    hass.config_entries._entries[original.entry_id] = original
    if identity_source == "live":
        hass.data[DOMAIN] = {
            original.entry_id: {
                DATA_KEY_COORDINATOR: NS(
                    get_switch_info=lambda: NS(mac="AA:BB:CC:DD:EE:FF")
                )
            }
        }
    result = await new_flow(hass).async_step_user(
        {**make_entry().data, "port": port, "name": "Different label"}
    )
    assert (
        result["type"] == FlowResultType.ABORT
        and result["reason"] == "already_configured"
    )


async def test_new_flow_saves_mac_identity_and_allows_distinct_device(
    hass, switch_server
):
    _, _, port = switch_server
    entry = make_entry(
        data={
            **make_entry().data,
            "host": "another-switch.example",
            CONF_SWITCH_MAC: "11:22:33:44:55:66",
        }
    )
    hass.config_entries._entries[entry.entry_id] = entry
    result = await new_flow(hass).async_step_user(
        {**make_entry().data, "port": port, "name": "New switch"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SWITCH_MAC] == "aa:bb:cc:dd:ee:ff"
    assert result["context"]["unique_id"] == "aa:bb:cc:dd:ee:ff"


async def test_legacy_entry_identity_backfill_preserves_unique_id(hass, switch_server):
    _, _, port = switch_server
    entry = make_entry(data={**make_entry().data, "port": port})
    hass.config_entries._entries[entry.entry_id] = entry
    before = (entry.entry_id, entry.unique_id)
    reload = AsyncMock()
    with (
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(hass.config_entries, "async_reload", reload),
    ):
        await async_setup_entry(hass, entry)
        assert entry.data[CONF_SWITCH_MAC] == "aa:bb:cc:dd:ee:ff"
        assert (entry.entry_id, entry.unique_id) == before
        reload.assert_not_awaited()
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, entry)
    await entry._async_process_on_unload(hass)


async def test_actual_ipv6_http_request():
    async def login(request):
        return web.Response(text='<script>var logonInfo=new Array("0");</script>')

    async def read(request):
        return web.Response(text="<html>OK</html>")

    app = web.Application()
    app.router.add_post("/logon.cgi", login)
    app.router.add_get("/test", read)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "::1", 0)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        pytest.skip("IPv6 loopback is unavailable on this test runner")
    port = site._server.sockets[0].getsockname()[1]
    api = TpLinkWebApi("::1", port, False, "admin", "test", False)
    try:
        assert await api.get("test") == "<html>OK</html>"
    finally:
        await api.disconnect()
        await runner.cleanup()


async def test_forms_serialize_for_frontend_and_validate_inputs(hass):
    import probatio
    from homeassistant.helpers import config_validation as cv
    from custom_components.tplink_easy_smart.config_flow import (
        TpLinkControllerOptionsFlowHandler,
    )

    result = await new_flow(hass).async_step_user()
    fields = probatio.to_field_list(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert (
        "text" in next(field for field in fields if field["name"] == "host")["selector"]
    )
    entry = make_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    flow = TpLinkControllerOptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.context = {"source": "init"}
    result = await flow.async_step_init()
    fields = probatio.to_field_list(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert fields[0]["selector"]["number"]["step"] == 1
    for invalid in (True, False, 1.9, 2.0):
        with pytest.raises(vol.Invalid):
            result["data_schema"]({"scan_interval": invalid})
    assert result["data_schema"]({"scan_interval": 30})["scan_interval"] == 30
