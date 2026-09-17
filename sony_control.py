#!/usr/bin/env python3
"""Small, dependency-free controller for the Sony STR-DN840."""

from __future__ import annotations

from enum import Enum
import http.client
import os
from pathlib import Path
import re
import socket
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

DEFAULT_HOST = "receiver.example"
CERS_PORT = 50001
IRCC_PORT = 8080
IRCC_PATH = "/upnp/control/IRCC"
RENDERING_PATH = "/RenderingControl/ctrl"

IRCC_SERVICE = "urn:schemas-sony-com:service:IRCC:1"
RENDERING_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"

POWER_TOGGLE = "AAAAAgAAADAAAAAVAQ=="
FUNCTION_PLUS = "AAAAAgAAALAAAABpAQ=="
DEVICE_ID_FILE = Path(__file__).resolve().with_name(".sony-control-device-id")


class SonyError(RuntimeError):
    pass


class RegistrationRequired(SonyError):
    pass


class PowerState(Enum):
    ON = "on"
    STANDBY = "standby"
    UNKNOWN = "unknown"


def device_id() -> str:
    configured = os.environ.get("SONY_DEVICE_ID")
    if configured:
        return configured
    try:
        saved = DEVICE_ID_FILE.read_text(encoding="ascii").strip()
        if saved:
            return saved
    except FileNotFoundError:
        pass
    node = uuid.getnode().to_bytes(6, "big")
    return "MediaRemote:" + "-".join(f"{byte:02X}" for byte in node)


def local_name(tag: str) -> str:
    return tag.rpartition("}")[2].rpartition(":")[2]


def parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        if "unbound prefix" not in str(error):
            raise SonyError(f"Receiver returned invalid XML: {error}") from error
        stripped = re.sub(r"<(/?)[A-Za-z0-9_.-]+:", r"<\1", text)
        return ET.fromstring(stripped)


def find_text(root: ET.Element, name: str) -> str | None:
    for element in root.iter():
        if local_name(element.tag) == name:
            return element.text
    return None


class Receiver:
    def __init__(self, host: str, timeout: float = 3.0):
        self.host = host
        self.timeout = timeout
        self.device_id = device_id()

    def _request(
        self,
        port: int,
        method: str,
        path: str,
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        connection = http.client.HTTPConnection(self.host, port, timeout=self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as error:
            raise SonyError(f"Could not reach {self.host}:{port}: {error}") from error
        finally:
            connection.close()

    def _soap(
        self, service: str, action: str, body: str, path: str
    ) -> ET.Element:
        envelope = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body>'
            "</s:Envelope>"
        )
        status, text = self._request(
            IRCC_PORT,
            "POST",
            path,
            envelope,
            {
                "soapaction": f'"{service}#{action}"',
                "content-type": "text/xml; charset=utf-8",
                "Connection": "close",
            },
        )
        if status != 200:
            error_code = None
            try:
                error_code = find_text(parse_xml(text), "errorCode")
            except (SonyError, ET.ParseError):
                pass
            detail = f" (UPnP error {error_code})" if error_code else ""
            raise SonyError(f"{action} failed with HTTP {status}{detail}")
        return parse_xml(text)

    def send_ircc(self, code: str) -> None:
        self._soap(
            IRCC_SERVICE,
            "X_SendIRCC",
            f"<IRCCCode>{code}</IRCCCode>",
            IRCC_PATH,
        )

    def volume(self) -> int:
        root = self._soap(
            RENDERING_SERVICE,
            "GetVolume",
            "<InstanceID>0</InstanceID><Channel>Master</Channel>",
            RENDERING_PATH,
        )
        value = find_text(root, "CurrentVolume")
        if value is None:
            raise SonyError("GetVolume returned no volume")
        return int(value)

    def power_state(self) -> PowerState:
        try:
            self.volume()
        except (SonyError, ValueError):
            return PowerState.UNKNOWN
        return PowerState.ON

    def is_awake(self) -> bool:
        return self.power_state() is PowerState.ON

    def cers_status(self) -> dict[str, str]:
        status, text = self._request(
            CERS_PORT,
            "GET",
            "/cers/getStatus",
            headers={
                "X-CERS-DEVICE-ID": self.device_id,
                "X-CERS-DEVICE-INFO": "Python/sony-control",
                "Connection": "close",
            },
        )
        if status in (401, 403):
            raise RegistrationRequired(
                "CERS registration is required; run 'sony-control register' "
                "while the receiver's TV SideView registration screen is open"
            )
        if status != 200:
            raise SonyError(f"getStatus failed with HTTP {status}")

        result: dict[str, str] = {}
        for element in parse_xml(text).iter():
            if local_name(element.tag) != "status":
                continue
            name = element.get("name")
            if name == "power" and element.get("value"):
                result["power"] = element.get("value", "")
            if name == "viewing":
                for item in element:
                    field = item.get("field")
                    value = item.get("value")
                    if field and value is not None:
                        result[field] = value
        return result

    def source(self) -> str:
        source = self.cers_status().get("source")
        if not source:
            raise SonyError("CERS status returned no source")
        return source

    def register(self) -> None:
        query = urllib.parse.urlencode(
            {
                "name": "sony-control",
                "registrationType": "initial",
                "deviceId": self.device_id,
            },
            quote_via=urllib.parse.quote,
            safe="",
        )
        path = f"/cers/register?{query}"
        headers = {
            "Host": f"{self.host}:{CERS_PORT}",
            "X-CERS-DEVICE-ID": self.device_id,
            "X-CERS-DEVICE-INFO": "Python/sony-control",
            "Connection": "close",
        }
        request = f"GET {path} HTTP/1.1\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in headers.items()
        ) + "\r\n"
        try:
            with socket.create_connection(
                (self.host, CERS_PORT), timeout=self.timeout
            ) as connection:
                connection.sendall(request.encode("ascii"))
                status_line = connection.makefile("rb").readline().decode(
                    "ascii", "replace"
                )
        except OSError as error:
            raise SonyError(f"Registration request failed: {error}") from error
        parts = status_line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise SonyError(f"Malformed registration response: {status_line!r}")
        status = int(parts[1])
        if status == 200:
            DEVICE_ID_FILE.write_text(self.device_id + "\n", encoding="ascii")
            return
        if status == 406:
            raise SonyError(
                "Open HOME NETWORK > OPTIONS > TV SideView Device Registration "
                "> Start Registration, then run this command within 30 seconds"
            )
        raise SonyError(f"Registration failed with HTTP {status}")

    def power_on(self, wait: float = 20.0) -> bool:
        if self.is_awake():
            return False
        self.send_ircc(POWER_TOGGLE)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self.is_awake():
                return True
        raise SonyError("Receiver did not become ready after the power command")

    def wake_from_standby(self, wait: float = 20.0) -> None:
        self.send_ircc(POWER_TOGGLE)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self.power_state() is PowerState.ON:
                return
        raise SonyError("Receiver did not become ready after the power command")

    def select_input(self, target: str, max_steps: int = 20) -> int:
        self.power_on()
        return self.select_input_when_awake(target, max_steps)

    def select_input_when_awake(self, target: str, max_steps: int = 20) -> int:
        current = self.source()
        if current.casefold() == target.casefold():
            return 0

        first = current
        for step in range(1, max_steps + 1):
            self.send_ircc(FUNCTION_PLUS)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                time.sleep(0.25)
                observed = self.source()
                if observed != current:
                    current = observed
                    break
            else:
                raise SonyError(f"Input did not change from {current}")
            if current.casefold() == target.casefold():
                return step
            if current == first:
                break
        raise SonyError(f"Input {target!r} was not found in the receiver's cycle")
