"""TP-Link api."""

import logging
from time import monotonic
import asyncio
import math
import re
from typing import Tuple

from .classes import (
    PoeClass,
    PoePowerLimit,
    PoePowerStatus,
    PoePriority,
    PoeState,
    PortPoeState,
    PortSpeed,
    PortState,
    TpLinkSystemInfo,
    PortStatistics,
)
from .const import (
    FEATURE_POE,
    FEATURE_STATS,
    URL_DEVICE_INFO,
    URL_POE_PORT_SETTINGS_SET,
    URL_POE_SETTINGS_GET,
    URL_POE_SETTINGS_SET,
    URL_PORT_SETTINGS_SET,
    URL_PORTS_SETTINGS_GET,
    URL_PORT_STATISTICS_GET,
    URL_PORT_STATISTICS_CLEAR,
)
from .coreapi import TpLinkWebApi, VariableType
from .utils import TpLinkFeaturesDetector

_LOGGER = logging.getLogger(__name__)

_POE_PRIORITIES_SET_MAP: dict[PoePriority, int] = {
    PoePriority.HIGH: 1,
    PoePriority.MIDDLE: 2,
    PoePriority.LOW: 3,
}

_POE_POWER_LIMITS_SET_MAP: dict[PoePowerLimit, Tuple[int, str | None]] = {
    PoePowerLimit.AUTO: (1, None),
    PoePowerLimit.CLASS_1: (2, "(4w)"),
    PoePowerLimit.CLASS_2: (3, "(7w)"),
    PoePowerLimit.CLASS_3: (4, "(15.4w)"),
    PoePowerLimit.CLASS_4: (5, "(30w)"),
}


def _integer(value, label, minimum=0):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}")
    try:
        number = int(value)
        if float(value) != number or number < minimum:
            raise ValueError(f"Invalid {label}")
    except (TypeError, ValueError, OverflowError) as ex:
        raise ValueError(f"Invalid {label}") from ex
    return number


def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as ex:
        raise ValueError(f"Invalid {label}") from ex
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Invalid {label}")
    return number


def _arrays(data, fields, count):
    values = [data.get(field) for field in fields]
    if any(not isinstance(items, list) or len(items) < count for items in values):
        raise ValueError("Incomplete port-state response")
    return values


# ---------------------------
#   ActionError
# ---------------------------
class ActionError(Exception):
    def __init__(self, message: str):
        """Initialize."""
        super().__init__(message)
        self._message = message

    def __str__(self, *args, **kwargs) -> str:
        """Return str(self)."""
        return f"{self._message}"

    def __repr__(self) -> str:
        """Return repr(self)."""
        return self.__str__()


# ---------------------------
#   TpLinkApi
# ---------------------------
class TpLinkApi:
    def __init__(
        self,
        host: str,
        port: int,
        use_ssl: bool,
        user: str,
        password: str,
        verify_ssl: bool,
    ) -> None:
        """Initialize."""
        self._core_api = TpLinkWebApi(host, port, use_ssl, user, password, verify_ssl)
        self._is_features_updated = False
        self._features_retry_at = 0.0
        self._features_lock = asyncio.Lock()
        self._features = TpLinkFeaturesDetector(self._core_api)
        _LOGGER.debug("New instance of TpLinkApi created")

    async def _ensure_features_updated(self):
        async with self._features_lock:
            if not self._is_features_updated and monotonic() >= self._features_retry_at:
                complete = await self._features.update()
                # Negative results also expire: an incomplete firmware page is not
                # enough evidence to suppress a feature for the whole HA session.
                self._is_features_updated = complete and all(
                    self._features.is_available(feature)
                    for feature in (FEATURE_POE, FEATURE_STATS)
                )
                self._features_retry_at = monotonic() + (300 if complete else 60)

    def is_feature_available_cached(self, feature: str) -> bool:
        """Return the last detected capability without network access."""
        return self._features.is_available(feature)

    async def is_feature_available(self, feature: str) -> bool:
        """Return true if specified feature is known and available."""
        await self._ensure_features_updated()
        return self._features.is_available(feature)

    async def authenticate(self) -> None:
        """Perform authentication."""
        await self._core_api.authenticate()

    async def disconnect(self) -> None:
        """Disconnect from api."""
        await self._core_api.disconnect()

    @property
    def device_url(self) -> str:
        """URL address of the device."""
        return self._core_api.device_url

    async def get_device_info(self) -> TpLinkSystemInfo:
        """Return the device information."""
        data = await self._core_api.get_variable(
            URL_DEVICE_INFO, "info_ds", VariableType.Dict
        )

        def get_value(key: str) -> str | None:
            if data is None:
                return None
            array = data.get(key, [])
            if not isinstance(array, list) or len(array) != 1:
                return None
            return array[0]

        mac = get_value("macStr")
        if (
            not isinstance(mac, str)
            or re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac) is None
        ):
            raise ValueError("Missing or invalid switch MAC address")
        return TpLinkSystemInfo(
            name=get_value("descriStr") or "TP-Link Switch",
            mac=mac,
            ip=get_value("ipStr"),
            netmask=get_value("netmaskStr"),
            gateway=get_value("gatewayStr"),
            firmware=get_value("firmwareStr"),
            hardware=get_value("hardwareStr"),
        )

    async def get_port_states(self) -> list[PortState]:
        """Return the port states."""
        data = await self._core_api.get_variables(
            URL_PORTS_SETTINGS_GET,
            [
                ("all_info", VariableType.Dict),
                ("max_port_num", VariableType.Int),
            ],
        )

        result: list[PortState] = []

        all_info = data.get("all_info")
        if not isinstance(all_info, dict):
            raise ValueError("Missing port information")
        max_port_num = _integer(data.get("max_port_num"), "port count", minimum=1)

        enabled_flags = all_info.get("state")
        speeds_config = all_info.get("spd_cfg")
        speeds_actual = all_info.get("spd_act")
        fc_config_flags = all_info.get("fc_cfg")
        fc_actual_flags = all_info.get("fc_act")

        arrays = (
            enabled_flags,
            speeds_config,
            speeds_actual,
            fc_config_flags,
            fc_actual_flags,
        )
        if any(
            not isinstance(values, list) or len(values) < max_port_num
            for values in arrays
        ):
            raise ValueError("Incomplete port-state response")

        for number in range(1, max_port_num + 1):
            state = PortState(
                number=number,
                speed_config=PortSpeed(
                    _integer(speeds_config[number - 1], "port enum")
                ),
                speed_actual=PortSpeed(
                    _integer(speeds_actual[number - 1], "port enum")
                ),
                enabled=_integer(enabled_flags[number - 1], "port flag") == 1,
                flow_control_config=_integer(fc_config_flags[number - 1], "port flag")
                == 1,
                flow_control_actual=_integer(fc_actual_flags[number - 1], "port flag")
                == 1,
            )
            result.append(state)

        return result

    async def get_port_statistics(self) -> list[PortStatistics]:
        """Return the port states."""
        if not await self.is_feature_available(FEATURE_STATS):
            return []
        data = await self._core_api.get_variables(
            URL_PORT_STATISTICS_GET,
            [
                ("all_info", VariableType.Dict),
                ("max_port_num", VariableType.Int),
            ],
        )

        result: list[PortStatistics] = []

        all_info = data.get("all_info")
        if not isinstance(all_info, dict):
            raise ValueError("Missing port information")
        max_port_num = _integer(data.get("max_port_num"), "port count", minimum=1)

        (pkts,) = _arrays(all_info, ("pkts",), max_port_num * 4)
        (enabled_flags,) = _arrays(all_info, ("state",), max_port_num)
        k = 0
        for number in range(1, max_port_num + 1):
            state = PortStatistics(
                number=number,
                enabled=_integer(enabled_flags[number - 1], "port flag") == 1,
                tx_good_pkts=_integer(pkts[k + 0], "packet counter"),
                tx_bad_pkts=_integer(pkts[k + 1], "packet counter"),
                rx_good_pkts=_integer(pkts[k + 2], "packet counter"),
                rx_bad_pkts=_integer(pkts[k + 3], "packet counter"),
            )
            k += 4
            result.append(state)

        return result

    async def clear_port_statistics(self) -> None:
        """Clear packet counters for all ports."""
        if not await self.is_feature_available(FEATURE_STATS):
            raise ActionError("Port statistics feature is not supported by device")

        # The switch web UI submits the Clear button as a GET request.
        await self._core_api.get(URL_PORT_STATISTICS_CLEAR, query="clear=Clear")

    async def get_port_poe_states(self) -> list[PortPoeState]:
        """Return the port states."""
        if not await self.is_feature_available(FEATURE_POE):
            return []

        data = await self._core_api.get_variables(
            URL_POE_SETTINGS_GET,
            [
                ("portConfig", VariableType.Dict),
                ("poe_port_num", VariableType.Int),
            ],
        )

        result: list[PortPoeState] = []

        port_config = data.get("portConfig")
        if not port_config:
            _LOGGER.debug("No portConfig found, returning")
            return result

        max_port_num = data.get("poe_port_num")
        if not max_port_num:
            _LOGGER.debug("No poe_port_num found, returning")
            return result

        state_flags = port_config.get("state")
        priority_flags = port_config.get("priority")
        powerlimit_flags = port_config.get("powerlimit")
        powers = port_config.get("power")
        currents = port_config.get("current")
        voltages = port_config.get("voltage")
        pdclass_flags = port_config.get("pdclass")
        powerstatus_flags = port_config.get("powerstatus")

        arrays = (
            state_flags,
            priority_flags,
            powerlimit_flags,
            powers,
            currents,
            voltages,
            pdclass_flags,
            powerstatus_flags,
        )
        if any(
            not isinstance(values, list) or len(values) < max_port_num
            for values in arrays
        ):
            raise ValueError("Incomplete PoE port-state response")

        for number in range(1, max_port_num + 1):
            state = PortPoeState(
                number=number,
                enabled=_integer(state_flags[number - 1], "port flag") == 1,
                priority=PoePriority(_integer(priority_flags[number - 1], "port enum")),
                current=_number(currents[number - 1], "PoE current"),
                voltage=_number(voltages[number - 1], "PoE value") / 10,
                power_limit=PoePowerLimit.try_parse(
                    _integer(powerlimit_flags[number - 1], "PoE limit")
                )
                or _number(powerlimit_flags[number - 1], "PoE value") / 10,
                power_status=PoePowerStatus(
                    _integer(powerstatus_flags[number - 1], "port enum")
                ),
                pd_class=PoeClass.try_parse(
                    _integer(pdclass_flags[number - 1], "PoE class")
                ),
                power=_number(powers[number - 1], "PoE power") / 10,
            )
            result.append(state)

        return result

    async def get_poe_state(self) -> PoeState | None:
        """Return the port states."""
        if not await self.is_feature_available(FEATURE_POE):
            return None

        _LOGGER.debug("Begin fetching POE states")

        poe_config = await self._core_api.get_variable(
            URL_POE_SETTINGS_GET, "globalConfig", VariableType.Dict
        )
        if not poe_config:
            _LOGGER.debug("No globalConfig found, returning")
            return None

        return PoeState(
            power_limit=_number(
                poe_config.get("system_power_limit"), "system_power_limit"
            )
            / 10,
            power_remain=_number(
                poe_config.get("system_power_remain"), "system_power_remain"
            )
            / 10,
            power_limit_min=_number(
                poe_config.get("system_power_limit_min"), "system_power_limit_min"
            )
            / 10,
            power_limit_max=_number(
                poe_config.get("system_power_limit_max"), "system_power_limit_max"
            )
            / 10,
            power_consumption=_number(
                poe_config.get("system_power_consumption"), "system_power_consumption"
            )
            / 10,
        )

    async def set_port_state(
        self,
        number: int,
        enabled: bool,
        speed_config: PortSpeed,
        flow_control_config: bool,
    ) -> None:
        """Change port state."""
        number = _integer(number, "port number", minimum=1)
        if number > len(await self.get_port_states()):
            raise ActionError("Port number exceeds switch port count")
        query: str = (
            f"portid={number}&"
            f"state={1 if enabled else 0}&"
            f"speed={speed_config.value}&"
            f"flowcontrol={1 if flow_control_config else 0}&"
            f"apply=Apply"
        )
        await self._core_api.get(URL_PORT_SETTINGS_SET, query=query)

    async def set_poe_limit(self, limit: float) -> None:
        """Change poe limit."""
        if not await self.is_feature_available(FEATURE_POE):
            raise ActionError("POE feature is not supported by device")

        limit = _number(limit, "PoE limit")
        current_state = await self.get_poe_state()
        if not current_state:
            raise ActionError("Can not get actual PoE state")

        if limit < current_state.power_limit_min:
            raise ActionError(
                f"PoE limit should be greater than or equal to {current_state.power_limit_min}"
            )
        if limit > current_state.power_limit_max:
            raise ActionError(
                f"PoE limit should be less than or equal to {current_state.power_limit_max}"
            )

        data = {
            "name_powerlimit": limit,
            "name_powerconsumption": current_state.power_consumption,
            "name_powerremain": current_state.power_remain,
            "applay": "Apply",
        }
        result = await self._core_api.post(URL_POE_SETTINGS_SET, data)
        _LOGGER.debug("POE_SET_RESULT: %s", result)

    async def set_port_poe_settings(
        self,
        port_number: int,
        enabled: bool,
        priority: PoePriority,
        power_limit: PoePowerLimit | float,
    ) -> None:
        if not await self.is_feature_available(FEATURE_POE):
            raise ActionError("POE feature is not supported by device")
        """Change port poe settings."""
        port_number = _integer(port_number, "port number", minimum=1)
        if port_number < 1:
            raise ActionError("Port number should be greater than or equals to 1")

        poe_ports_count = await self._core_api.get_variable(
            URL_POE_SETTINGS_GET, "poe_port_num", VariableType.Int
        )
        if not poe_ports_count:
            raise ActionError("Can not get PoE ports count")

        if port_number > poe_ports_count:
            raise ActionError(
                f"Port number should be less than or equals to {poe_ports_count}"
            )

        pstate = 2 if enabled else 1

        ppriority = _POE_PRIORITIES_SET_MAP.get(priority)
        if not ppriority:
            raise ActionError("Invalid PoePriority specified")

        if isinstance(power_limit, PoePowerLimit):
            ppowerlimit, ppowerlimit2 = _POE_POWER_LIMITS_SET_MAP.get(power_limit)
            if not ppowerlimit:
                raise ActionError("Invalid PoePowerLimit specified")
        elif isinstance(power_limit, (float, int)) and not isinstance(
            power_limit, bool
        ):
            power_limit = _number(power_limit, "manual PoE limit")
            if 0.1 <= power_limit <= 30.0:  # hardcoded in Tp-Link javascript
                ppowerlimit = 6
                ppowerlimit2 = power_limit
            else:
                raise ActionError("Power limit must be in range of 0.1-30.0")
        else:
            raise ActionError("Invalid power_limit specified")

        data = {
            "name_pstate": pstate,
            "name_ppriority": ppriority,
            "name_ppowerlimit": ppowerlimit,
            "name_ppowerlimit2": ppowerlimit2,
            f"sel_{port_number}": 1,
            "applay": "Apply",
        }
        result = await self._core_api.post(URL_POE_PORT_SETTINGS_SET, data)
        _LOGGER.debug("POE_PORT_SETTINGS_SET_RESULT: %s", result)
