"""Focused source-level regressions; run with python -m unittest discover -s tests.

These tests execute production functions in isolated namespaces so they can run
without a Home Assistant installation. They do not replace HA or hardware tests.
"""
import ast
import asyncio
from functools import wraps
from pathlib import Path
import re
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1] / "custom_components/tplink_easy_smart"


def load(file, names, scope=None):
    """Execute selected production definitions, preserving their actual bodies."""
    scope = dict(scope or {})
    tree = ast.parse((ROOT / file).read_text())
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    assert len(nodes) == len(names), (file, names)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), file, "exec"), scope)
    return scope


CLASSES = load("client/classes.py", ["PortSpeed", "PoePriority", "PoePowerLimit", "PoeClass", "PoePowerStatus", "PortState", "PortStatistics", "PortPoeState"], {"IntEnum": __import__("enum").IntEnum, "dataclass": __import__("dataclasses").dataclass})
CORE = load("client/coreapi.py", ["ApiCallError", "AuthenticationError", "_to_list", "TpLinkWebApi"], {"re": re, "_ARRAY_VALUES_REGEX": next(ast.literal_eval(n.value) for n in ast.parse((ROOT / "client/coreapi.py").read_text()).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_ARRAY_VALUES_REGEX" for t in n.targets)), "_LOGGER": Mock()})
FEATURE_SCOPE = {"wraps": wraps, "ApiCallError": CORE["ApiCallError"], "APICALL_ERRCAT_DISCONNECTED": "disconnected", "FEATURE_POE": "poe", "FEATURE_STATS": "stats", "_LOGGER": Mock()}
DETECTOR = load("client/utils.py", ["TpLinkFeaturesDetector"], FEATURE_SCOPE)["TpLinkFeaturesDetector"]
API_SCOPE = {**CLASSES, "_LOGGER": Mock(), "FEATURE_POE": "poe", "FEATURE_STATS": "stats", "VariableType": NS(Dict=1, Int=2), "URL_PORTS_SETTINGS_GET": "ports", "URL_POE_SETTINGS_GET": "poe", "URL_PORT_STATISTICS_GET": "stats", "TpLinkWebApi": Mock(), "TpLinkFeaturesDetector": DETECTOR}


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_and_invalid_arrays(self):
        parse = CORE["_to_list"]
        self.assertEqual(list(parse("new Array()")), [])
        self.assertEqual(list(parse("new Array(  )")), [])
        self.assertEqual(list(parse('new Array("0","1")')), ["0", "1"])
        with self.assertRaises(ValueError):
            list(parse("invalid"))

    async def test_port_snapshot_validation(self):
        scope = dict(API_SCOPE)
        api_cls = load("client/tplink_api.py", ["TpLinkApi"], scope)["TpLinkApi"]
        api = object.__new__(api_cls)
        api._core_api = NS(get_variables=AsyncMock())
        valid = {"state": [1, 0], "spd_cfg": [1, 1], "spd_act": [6, 0], "fc_cfg": [0, 1], "fc_act": [0, 1]}
        for field in valid:
            for bad in (None, [], [1]):
                data = {**valid, field: bad}
                api._core_api.get_variables.return_value = {"all_info": data, "max_port_num": 2}
                with self.assertRaises(ValueError):
                    await api.get_port_states()
        api._core_api.get_variables.return_value = {"all_info": valid, "max_port_num": 2}
        ports = await api.get_port_states()
        self.assertEqual([p.number for p in ports], [1, 2])
        self.assertFalse(ports[1].enabled)

    async def test_poe_snapshot_validation(self):
        api_cls = load("client/tplink_api.py", ["TpLinkApi"], API_SCOPE)["TpLinkApi"]
        api = object.__new__(api_cls)
        api.is_feature_available = AsyncMock(return_value=True)
        api._core_api = NS(get_variables=AsyncMock())
        valid = {"state": [1], "priority": [0], "powerlimit": [330], "power": [10], "current": [20], "voltage": [500], "pdclass": [40], "powerstatus": [2]}
        for field in valid:
            api._core_api.get_variables.return_value = {"portConfig": {**valid, field: []}, "poe_port_num": 1}
            with self.assertRaises(ValueError):
                await api.get_port_poe_states()
        api._core_api.get_variables.return_value = {"portConfig": valid, "poe_port_num": 1}
        self.assertEqual((await api.get_port_poe_states())[0].voltage, 50)

    async def test_failed_probe_recovers_without_repeated_requests(self):
        now = [100.0]
        api_cls = load("client/tplink_api.py", ["TpLinkApi"], {**API_SCOPE, "monotonic": lambda: now[0]})["TpLinkApi"]
        api = object.__new__(api_cls)
        api._is_features_updated = False
        api._features_retry_at = 0
        detector = DETECTOR(NS())
        detector._is_poe_available = AsyncMock(side_effect=[None, True])
        detector._is_stats_available = AsyncMock(return_value=True)
        api._features = detector
        self.assertFalse(await api.is_feature_available("poe"))
        self.assertTrue(await api.is_feature_available("stats"))
        self.assertFalse(api._is_features_updated)
        detector._is_poe_available.assert_awaited_once()
        now[0] = 161
        self.assertTrue(await api.is_feature_available("poe"))
        self.assertTrue(api._is_features_updated)
        await api.is_feature_available("poe")
        self.assertEqual(detector._is_poe_available.await_count, 2)
        # An explicitly unsupported feature is a completed probe, not a failure.
        detector._is_poe_available = AsyncMock(return_value=False)
        self.assertTrue(await detector.update())
        self.assertFalse(detector.is_available("poe"))

    async def test_disconnected_probe_is_unknown_other_errors_propagate(self):
        @DETECTOR.disconnected_as_unknown
        async def probe(error):
            raise error
        self.assertIsNone(await probe(CORE["ApiCallError"]("offline", -4, "disconnected")))
        with self.assertRaises(CORE["ApiCallError"]):
            await probe(CORE["ApiCallError"]("bad response", -3, "request_error"))

    async def test_first_refresh_rejects_missing_ports(self):
        class Base:
            pass
        cls = load("update_coordinator.py", ["TpLinkDataUpdateCoordinator"], {"DataUpdateCoordinator": Base, "UpdateFailed": RuntimeError, "_LOGGER": Mock()})["TpLinkDataUpdateCoordinator"]
        obj = object.__new__(cls)
        obj._update_switch_info = AsyncMock()
        obj._update_port_states = AsyncMock()
        obj._port_states = []
        obj._update_poe_state = AsyncMock()
        obj._update_port_poe_states = AsyncMock()
        obj._update_port_statistics = AsyncMock()
        with self.assertRaises(RuntimeError):
            await obj.async_update()
        obj._port_states = [1]
        await obj.async_update()
        obj._update_port_statistics.assert_awaited_once()

    async def test_failed_or_cancelled_setup_closes_and_removes_coordinator(self):
        for error in (RuntimeError("offline"), asyncio.CancelledError()):
            obj = NS(async_config_entry_first_refresh=AsyncMock(side_effect=error), async_unload=AsyncMock())
            hass = NS(data={})
            def pop(hass, entry):
                return hass.data["test"][entry.entry_id].pop("coordinator")
            setup = load("__init__.py", ["async_setup_entry"], {"TpLinkDataUpdateCoordinator": lambda *args: obj, "DOMAIN": "test", "DATA_KEY_COORDINATOR": "coordinator", "pop_coordinator": pop})["async_setup_entry"]
            with self.assertRaises(type(error)):
                await setup(hass, NS(entry_id="one"))
            obj.async_unload.assert_awaited_once()
            self.assertNotIn("coordinator", hass.data["test"]["one"])

    def test_mac_lookup_is_case_insensitive_and_handles_missing_info(self):
        class Coordinator:
            def __init__(self, mac):
                self.mac = mac
            def get_switch_info(self):
                return NS(mac=self.mac) if self.mac else None
        lookup = load("services.py", ["_find_coordinator"], {"_LOGGER": Mock(), "DOMAIN": "test", "DATA_KEY_SERVICES": "services", "DATA_KEY_COORDINATOR": "coordinator", "TpLinkDataUpdateCoordinator": Coordinator})["_find_coordinator"]
        target = Coordinator("aa:bb:cc:dd:ee:ff")
        hass = NS(data={"test": {"services": 2, "empty": {"coordinator": Coordinator(None)}, "target": {"coordinator": target}}})
        self.assertIs(lookup(hass, "AA:BB:CC:DD:EE:FF"), target)
        self.assertIsNone(lookup(hass, "11:22:33:44:55:66"))

    async def test_disconnect_resets_initialization(self):
        cls = CORE["TpLinkWebApi"]
        obj = object.__new__(cls)
        obj._session = NS(close=AsyncMock())
        obj._is_initialized = True
        await obj.disconnect()
        self.assertIsNone(obj._session)
        self.assertFalse(obj._is_initialized)

    async def test_recovered_entities_added_once_on_all_platforms(self):
        for file, initial, recovered in (("binary_sensor.py", 2, 5), ("switch.py", 2, 5), ("sensor.py", 1, 2), ("button.py", 0, 1)):
            with self.subTest(platform=file):
                callbacks, cleanup, added = [], [], []
                capabilities = set()
                coordinator = NS(ports_count=2, ports_poe_count=0, get_switch_info=lambda: NS(name="Test"), is_feature_available_cached=lambda f: f in capabilities)
                def listen(cb):
                    callbacks.append(cb)
                    return lambda: callbacks.remove(cb)
                coordinator.async_add_listener = listen
                entry = NS(options={"ports": True, "poe": True}, async_on_unload=cleanup.append)
                def entity(kind):
                    return lambda coordinator, description=None: NS(kind=kind, description=description)
                scope = {"callback": lambda f: f, "get_coordinator": lambda *args: coordinator, "FEATURE_POE": "poe", "FEATURE_STATS": "stats", "OPT_PORT_STATE_SWITCHES": "ports", "OPT_POE_STATE_SWITCHES": "poe", "DEFAULT_PORT_STATE_SWITCHES": False, "DEFAULT_POE_STATE_SWITCHES": False, "BinarySensorDeviceClass": NS(CONNECTIVITY=1, POWER=2), "SensorDeviceClass": NS(POWER=1), "SensorStateClass": NS(MEASUREMENT=1), "UnitOfPower": NS(WATT="W")}
                for name in ("_FUNCTION_UID_PORT_STATE_FORMAT", "_FUNCTION_DISPLAYED_NAME_PORT_STATE_FORMAT", "_FUNCTION_UID_PORT_POE_STATE_FORMAT", "_FUNCTION_DISPLAYED_NAME_PORT_POE_STATE_FORMAT"):
                    scope[name] = name + " {}"
                for name in ("_FUNCTION_UID_NETWORK_INFO", "_FUNCTION_DISPLAYED_NAME_NETWORK_INFO", "_FUNCTION_UID_POE_INFO", "_FUNCTION_DISPLAYED_NAME_POE_INFO"):
                    scope[name] = name
                for name in ("TpLinkPortBinarySensorEntityDescription", "TpLinkPortSwitchEntityDescription", "TpLinkSensorEntityDescription"):
                    scope[name] = lambda **kwargs: NS(**kwargs)
                for name in ("TpLinkPortStateBinarySensor", "TpLinkPortPoeStateBinarySensor", "TpLinkPortStateSwitch", "TpLinkPortPoeStateSwitch", "TpLinkNetworkInfoSensor", "TpLinkPoeInfoSensor", "TpLinkClearPortStatisticsButton"):
                    scope[name] = entity(name)
                setup = load(file, ["async_setup_entry"], scope)["async_setup_entry"]
                await setup(NS(), entry, lambda entities: added.extend(entities))
                self.assertEqual(len(added), initial)
                capabilities.update(("poe", "stats"))
                coordinator.ports_count = 3
                coordinator.ports_poe_count = 2
                for cb in callbacks:
                    cb()
                    cb()
                self.assertEqual(len(added), recovered)
                for cb in cleanup:
                    cb()
                self.assertFalse(callbacks)


if __name__ == "__main__":
    unittest.main()
