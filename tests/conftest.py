"""Real Home Assistant and local simulated-switch fixtures."""

from types import MappingProxyType
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from aiohttp import web
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from custom_components.tplink_easy_smart.client.tplink_api import TpLinkApi
from custom_components.tplink_easy_smart.const import DOMAIN


def make_entry(**overrides):
    values = dict(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="Test Switch",
        unique_id=None,
        data={
            "name": "Test",
            "host": "127.0.0.1",
            "port": 80,
            "ssl": False,
            "username": "admin",
            "password": "test",
            "verify_ssl": False,
        },
        options={},
        source="user",
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )
    values.update(overrides)
    return ConfigEntry(**values)


@pytest.fixture
async def hass(tmp_path):
    obj = HomeAssistant(str(tmp_path))
    obj.config_entries = ConfigEntries(obj, {})
    yield obj
    await obj.async_stop(force=True)


@pytest.fixture
async def switch_server():
    state = {
        "logins": 0,
        "expired": False,
        "enabled": [1, 0],
        "poe_enabled": [1, 0],
        "poe_status": [2, 0],
        "packets": [100, 0, 200, 0, 20, 1, 30, 2],
        "status": {},
        "clears": 0,
        "writes": [],
        "login_code": "0",
    }

    def page(**variables):
        import json

        return (
            "<html><SCRIPT type='text/javascript'>"
            + "".join(
                f"var {key} = {json.dumps(value)};" for key, value in variables.items()
            )
            + "</SCRIPT></html>"
        )

    async def handle(request):
        path = request.path[1:]
        if path == "logon.cgi":
            state["logins"] += 1
            response = web.Response(
                text='<script>var logonInfo = new Array("'
                + state["login_code"]
                + '");</script>'
            )
            response.set_cookie("session", "valid")
            return response
        if path in state["status"]:
            return web.Response(status=state["status"][path], text="server error")
        if state["expired"] or request.cookies.get("session") != "valid":
            state["expired"] = False
            return web.Response(text='<script>var logonInfo = new Array("5");</script>')
        if path == "SystemInfoRpm.htm":
            return web.Response(
                text=page(
                    info_ds={
                        "descriStr": ["Test; Switch"],
                        "macStr": ["aa:bb:cc:dd:ee:ff"],
                        "ipStr": ["127.0.0.1"],
                        "maskStr": ["255.255.255.0"],
                        "firmwareStr": ["test"],
                        "hardwareStr": ["V5"],
                    }
                )
            )
        if path == "PortSettingRpm.htm":
            return web.Response(
                text=page(
                    all_info={
                        "state": state["enabled"],
                        "spd_cfg": [1, 1],
                        "spd_act": [6, 0],
                        "fc_cfg": [0, 0],
                        "fc_act": [0, 0],
                    },
                    max_port_num=2,
                )
            )
        if path == "PortStatisticsRpm.htm":
            return web.Response(
                text=page(
                    all_info={"state": state["enabled"], "pkts": state["packets"]},
                    max_port_num=2,
                )
            )
        if path == "PoeConfigRpm.htm":
            return web.Response(
                text=page(
                    portConfig={
                        "state": state["poe_enabled"],
                        "priority": [0, 1],
                        "powerlimit": [330, 40],
                        "power": [10, 0],
                        "current": [20, 0],
                        "voltage": [500, 0],
                        "pdclass": [40, 70],
                        "powerstatus": state["poe_status"],
                    },
                    poe_port_num=2,
                    globalConfig={
                        "system_power_limit": 600,
                        "system_power_remain": 590,
                        "system_power_limit_min": 10,
                        "system_power_limit_max": 650,
                        "system_power_consumption": 10,
                    },
                )
            )
        if path == "port_statistics_set.cgi":
            state["clears"] += 1
            state["packets"] = [0] * 8
            return web.Response(text="<html>OK</html>")
        if path == "port_setting.cgi":
            state["enabled"][int(request.query["portid"]) - 1] = int(
                request.query["state"]
            )
            return web.Response(text="<html>OK</html>")
        if path in ("poe_global_config.cgi", "poe_port_config.cgi"):
            state["writes"].append(dict(await request.post()))
            return web.Response(text="<html>OK</html>")
        return web.Response(status=404, text="unsupported")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    api = TpLinkApi("127.0.0.1", port, False, "admin", "test", False)
    yield api, state, port
    await api.disconnect()
    await runner.cleanup()
