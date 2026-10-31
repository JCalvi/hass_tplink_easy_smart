"""Exercise the real HTTP client against local aiohttp responses."""

import asyncio
from unittest.mock import AsyncMock
import pytest
from custom_components.tplink_easy_smart.client import coreapi
from custom_components.tplink_easy_smart.client.coreapi import (
    ApiCallError,
    AuthenticationError,
    VariableType,
)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("<script>var a=1; var b=2;</script>", {"a": "1", "b": "2"}),
        (
            '<SCRIPT type="text/javascript">var a={name:"a;b", list:[1,2]};var b="x\\";y";</SCRIPT>',
            {"a": '{name:"a;b", list:[1,2]}', "b": '"x\\";y"'},
        ),
        ("<script>var a=1;</script><script>var b=2;</script>", {"a": "1", "b": "2"}),
        ("<html>bad response</html>", {}),
    ],
)
def test_javascript_values(source, expected):
    assert coreapi._get_variables(source) == expected


@pytest.mark.parametrize(
    "source,expected",
    [
        ("new Array()", []),
        ("new Array(  )", []),
        ("new Array(\"a,b\", 'x', 0)", ["a,b", "x", "0"]),
    ],
)
def test_arrays(source, expected):
    assert list(coreapi._to_list(source)) == expected


@pytest.mark.parametrize("source", ["bad", "new Array({a:1})", "new Array(null)"])
def test_invalid_array(source):
    with pytest.raises(ValueError):
        list(coreapi._to_list(source))


async def test_http_data_authentication_and_reset(switch_server):
    api, state, _ = switch_server
    info = await api.get_device_info()
    assert info.mac == "aa:bb:cc:dd:ee:ff"
    assert info.name == "Test; Switch"
    assert state["logins"] == 1
    ports = await api.get_port_states()
    assert len(ports) == 2
    assert ports[0].enabled
    assert not ports[1].enabled
    stats = await api.get_port_statistics()
    assert stats[0].rx_good_pkts == 200
    assert stats[1].tx_bad_pkts == 1
    poe = await api.get_port_poe_states()
    assert poe[0].voltage == 50
    assert poe[0].power == 1
    state["expired"] = True
    await api.get_device_info()
    assert state["logins"] == 2
    await api.clear_port_statistics()
    assert state["clears"] == 1
    assert all(s.rx_good_pkts == 0 for s in await api.get_port_statistics())


@pytest.mark.parametrize("status", [404, 500, 502])
async def test_non_auth_http_error_does_not_relogin(switch_server, status):
    api, state, _ = switch_server
    await api.get_device_info()
    state["status"]["bad"] = status
    with pytest.raises(ApiCallError):
        await api._core_api.get("bad")
    assert state["logins"] == 1


@pytest.mark.parametrize(
    "code,reason",
    [
        ("1", "auth_invalid_credentials"),
        ("2", "auth_user_blocked"),
        ("3", "auth_too_many_users"),
        ("5", "auth_session_timeout"),
        ("99", "auth_general"),
    ],
)
async def test_login_rejections(switch_server, code, reason):
    api, state, _ = switch_server
    state["login_code"] = code
    with pytest.raises(AuthenticationError) as ex:
        await api.get_device_info()
    assert ex.value.reason_code == reason


async def test_disconnect_reconnect_and_concurrency(switch_server):
    api, state, _ = switch_server
    await asyncio.gather(
        api.get_device_info(), api.get_port_states(), api.get_device_info()
    )
    assert state["logins"] == 1
    await api.disconnect()
    assert not api._core_api._is_initialized
    await api.get_device_info()
    assert state["logins"] == 2


async def test_get_variable_forwards_request_options(switch_server):
    api, _, _ = switch_server
    api._core_api.get = AsyncMock(return_value="<script>var n=1;</script>")
    assert (
        await api._core_api.get_variable(
            "test", "n", VariableType.Int, check_authorized="checker"
        )
        == 1
    )
    api._core_api.get.assert_awaited_once_with("test", check_authorized="checker")
