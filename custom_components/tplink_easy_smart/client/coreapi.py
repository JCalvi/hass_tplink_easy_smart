"""Authenticated HTTP transport and parsing for the Easy Smart web interface."""

import asyncio
from enum import Enum
from html.parser import HTMLParser
import logging
import re
from typing import Any, Final, Iterable, TypeAlias

import aiohttp
import json5
from yarl import URL

TIMEOUT: Final = 5.0
APICALL_ERRCODE_UNAUTHORIZED = -2
APICALL_ERRCODE_REQUEST = -3
APICALL_ERRCODE_DISCONNECTED = -4
APICALL_ERRCAT_CREDENTIALS = "user_pass_err"
APICALL_ERRCAT_REQUEST = "request_error"
APICALL_ERRCAT_UNAUTHORIZED = "unauthorized"
APICALL_ERRCAT_DISCONNECTED = "disconnected"
AUTH_FAILURE_GENERAL = "auth_general"
AUTH_FAILURE_CREDENTIALS = "auth_invalid_credentials"
AUTH_USER_BLOCKED = "auth_user_blocked"
AUTH_TOO_MANY_USERS = "auth_too_many_users"
AUTH_SESSION_TIMEOUT = "auth_session_timeout"
_VAR_LOGON_INFO = "logonInfo"
_ARRAY_VALUES_REGEX = r"\s*new\s*Array\s*\((?P<items>.*)\)\s*"
_VARIABLES_REGEX = re.compile(r"\bvar\s+([a-zA-Z_$][\w$]*)\s*=")
_LOGGER = logging.getLogger(__name__)
VariableValue: TypeAlias = str | int | list[str] | dict[str, Any]


class VariableType(Enum):
    Str = 0
    Int = 1
    List = 2
    Dict = 3


class AuthenticationError(Exception):
    def __init__(self, message: str, reason_code: str):
        super().__init__(message)
        self._reason_code = reason_code

    @property
    def reason_code(self):
        return self._reason_code


class ApiCallError(Exception):
    def __init__(
        self,
        message: str,
        error_code: int | None,
        error_category: str | None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self._error_code = error_code
        self._error_category = error_category
        self.http_status = http_status

    @property
    def code(self):
        return self._error_code

    @property
    def category(self) -> str | None:
        return self._error_category


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._parts = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._parts = []

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


def _script_views(script):
    """Mask strings for declaration search and remove comments from values."""
    code, values = list(script), list(script)
    quote = None
    escaped = False
    comment = None
    index = 0
    while index < len(script):
        char = script[index]
        pair = script[index : index + 2]
        if comment:
            if comment == "line" and char in "\r\n":
                comment = None
            elif comment == "block" and pair == "*/":
                code[index : index + 2] = values[index : index + 2] = "  "
                index += 2
                comment = None
                continue
            else:
                code[index] = values[index] = " " if char not in "\r\n" else char
        elif quote:
            code[index] = " "
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif pair in ("//", "/*"):
            comment = "line" if pair == "//" else "block"
            code[index : index + 2] = values[index : index + 2] = "  "
            index += 2
            continue
        elif char in "\"'`":
            quote = char
            code[index] = " "
        index += 1
    return "".join(code), "".join(values)


def _get_variables(page: str) -> dict[str, str]:
    """Extract literal declarations without executing JavaScript."""
    parser = _Scripts()
    parser.feed(page)
    result = {}
    for script in parser.scripts:
        code, values = _script_views(script)
        position = 0
        while match := _VARIABLES_REGEX.search(code, position):
            start = match.end()
            depth = 0
            end = start
            for end in range(start, len(code)):
                char = code[end]
                if char in "([{":
                    depth += 1
                elif char in ")]}":
                    depth -= 1
                elif char == ";" and depth == 0:
                    break
            else:
                end = len(code)
            value = values[start:end].strip()
            if value and depth == 0:
                result[match.group(1)] = value
            position = max(end + 1, start + 1)
    return result


def _to_list(array_data: str) -> Iterable[str]:
    match = re.fullmatch(_ARRAY_VALUES_REGEX, array_data, re.DOTALL)
    if match is None:
        raise ValueError("Invalid JavaScript array value")
    values = json5.loads("[" + match.group("items") + "]")
    if any(not isinstance(value, (str, int, float)) for value in values):
        raise ValueError("Invalid JavaScript array item")
    return [str(value) for value in values]


def _to_dict(json_data: str) -> dict[str, Any] | None:
    if not json_data:
        return None
    value = json5.loads(json_data)
    if not isinstance(value, dict):
        raise ValueError("Expected a JavaScript object")
    return value


def _convert_value(value: str | None, variable_type: VariableType):
    if value is None:
        return None
    if variable_type == VariableType.Str:
        return json5.loads(value) if value.startswith(("'", '"')) else value
    if variable_type == VariableType.Int:
        return int(value)
    if variable_type == VariableType.List:
        return list(_to_list(value))
    if variable_type == VariableType.Dict:
        return _to_dict(value)
    raise ValueError("Unknown variable type")


def _get_variable(page, name, variable_type):
    return _convert_value(_get_variables(page).get(name), variable_type)


def _is_login_page(result):
    return _VAR_LOGON_INFO in _get_variables(result) or bool(
        re.search(
            r"<form\b[^>]*\baction\s*=\s*['\"]?/?logon\.cgi", result, re.IGNORECASE
        )
    )


def _check_authorized(response, result):
    return (
        response.status == 200 and bool(result.strip()) and not _is_login_page(result)
    )


async def _get_response_text(response):
    try:
        return (await response.content.read()).decode("utf-8")
    finally:
        response.release()


class TpLinkWebApi:
    def __init__(self, host, port, use_ssl, user, password, verify_ssl):
        self._user = user
        self._password = password
        self._verify_ssl = verify_ssl
        self._session = None
        self._is_initialized = False
        self._call_locker = asyncio.Lock()
        schema = "https" if use_ssl else "http"
        # URL.build brackets IPv6 literals and preserves zone identifiers.
        host = host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        self._base_url = str(URL.build(scheme=schema, host=host, port=port))

    @property
    def device_url(self):
        return self._base_url

    def _get_url(self, path):
        return self._base_url + "/" + path

    async def _ensure_initialized(self):
        if not self._is_initialized:
            await self.authenticate()

    async def _raw_request(self, method, path, data=None):
        try:
            return await self._session.request(
                method,
                self._get_url(path),
                data=data,
                allow_redirects=True,
                ssl=None if self._verify_ssl else False,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            )
        except aiohttp.ServerDisconnectedError as ex:
            raise ApiCallError(
                f"Disconnected during {method} {path}",
                APICALL_ERRCODE_DISCONNECTED,
                APICALL_ERRCAT_DISCONNECTED,
            ) from ex
        except (aiohttp.ClientError, TimeoutError, OSError) as ex:
            raise ApiCallError(
                f"Failed {method} {path}: {type(ex).__name__}",
                APICALL_ERRCODE_REQUEST,
                APICALL_ERRCAT_REQUEST,
            ) from ex

    async def _get_raw(self, path):
        return await self._raw_request("GET", path)

    async def _post_raw(self, path, data):
        return await self._raw_request("POST", path, data)

    def _refresh_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        self._session.cookie_jar.clear()

    async def authenticate(self):
        self._is_initialized = False
        self._refresh_session()
        try:
            response = await self._post_raw(
                "logon.cgi",
                {"username": self._user, "password": self._password, "logon": "Login"},
            )
            result = await _get_response_text(response)
            if response.status != 200:
                raise AuthenticationError("Login request failed", AUTH_FAILURE_GENERAL)
            items = _get_variable(result, _VAR_LOGON_INFO, VariableType.List)
            if not items:
                raise AuthenticationError("Missing login result", AUTH_FAILURE_GENERAL)
            reasons = {
                "1": AUTH_FAILURE_CREDENTIALS,
                "2": AUTH_USER_BLOCKED,
                "3": AUTH_TOO_MANY_USERS,
                "4": AUTH_TOO_MANY_USERS,
                "5": AUTH_SESSION_TIMEOUT,
            }
            if items[0] != "0":
                raise AuthenticationError(
                    "Login rejected", reasons.get(items[0], AUTH_FAILURE_GENERAL)
                )
            self._is_initialized = True
        except AuthenticationError:
            raise
        except (
            ApiCallError,
            aiohttp.ClientError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ) as ex:
            raise AuthenticationError(
                "Unable to read login response", AUTH_FAILURE_GENERAL
            ) from ex

    async def _request(self, method, path, data=None, check_authorized=None):
        async with self._call_locker:
            await self._ensure_initialized()
            checker = check_authorized or _check_authorized
            for attempt in range(2):
                response = await (
                    self._get_raw(path)
                    if method == "GET"
                    else self._post_raw(path, data)
                )
                try:
                    result = await _get_response_text(response)
                except (aiohttp.ClientError, TimeoutError, UnicodeError) as ex:
                    raise ApiCallError(
                        "Unable to read switch response",
                        APICALL_ERRCODE_REQUEST,
                        APICALL_ERRCAT_REQUEST,
                    ) from ex
                if checker(response, result):
                    return result
                needs_login = response.status in (401, 403) or (
                    response.status == 200 and _is_login_page(result)
                )
                if not needs_login:
                    raise ApiCallError(
                        f"Invalid response from {path}: HTTP {response.status}",
                        APICALL_ERRCODE_REQUEST,
                        APICALL_ERRCAT_REQUEST,
                        response.status,
                    )
                if attempt == 0:
                    await self.authenticate()
            self._is_initialized = False
            raise ApiCallError(
                "Switch session remains unauthorized",
                APICALL_ERRCODE_UNAUTHORIZED,
                APICALL_ERRCAT_UNAUTHORIZED,
            )

    async def get(self, path, query=None, **kwargs):
        relative_url = path if not query else f"{path}?{query}"
        return await self._request(
            "GET", relative_url, check_authorized=kwargs.get("check_authorized")
        )

    async def post(self, path, data=None, **kwargs):
        return await self._request("POST", path, data, kwargs.get("check_authorized"))

    async def get_variables(self, path, variables, **kwargs):
        values = _get_variables(await self.get(path, **kwargs))
        return {
            name: _convert_value(values.get(name), kind) for name, kind in variables
        }

    async def get_variable(self, path, variable, variable_type, **kwargs):
        return (
            await self.get_variables(path, [(variable, variable_type)], **kwargs)
        ).get(variable)

    async def disconnect(self):
        async with self._call_locker:
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._is_initialized = False
