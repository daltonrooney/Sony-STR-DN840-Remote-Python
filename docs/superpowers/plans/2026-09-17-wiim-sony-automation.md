# WiiM Mini to Sony STR-DN840 Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local daemon that detects AirPlay playback beginning on a WiiM Mini and, only when the Sony was positively identified as being in standby, wakes it and confirms SA-CD/CD as the selected input.

**Architecture:** Extract the proven Sony protocol code into an importable module, add a strict WiiM HTTP client, and keep transition policy in a separately testable controller. A synchronous polling service contains transient failures, while systemd supervises fatal process failures; live WiiM and Sony standby characterization gates service enablement.

**Tech Stack:** Python 3.12 standard library, `unittest`, WiiM local HTTP API, Sony CERS/IRCC/UPnP, systemd

**Spec:** `docs/superpowers/specs/2026-09-17-wiim-sony-automation-design.md`

## Global Constraints

- Keep all device control local to the LAN.
- Use the existing verified Sony CERS registration, IRCC commands, readiness probe, and feedback-driven input selection.
- Initial policy is `switch_if_off`: an already-on Sony keeps its current input.
- Never toggle Sony power from a failed request alone or from `UNKNOWN` power state.
- Never cycle Sony inputs unless the current input is known; confirm the target through CERS.
- Treat the first valid AirPlay `play` after startup as actionable.
- Preserve the last valid WiiM state through transport and parsing failures.
- Do not add automatic power-off, volume synchronization, CEC control, Home Assistant, MQTT, cloud control, playback control, or multiroom behavior.
- Use only the Python standard library; do not add runtime dependencies.
- Do not enable the systemd service until live-device validation passes.
- Keep `.sony-control-device-id` local and ignored by Git.

---

## File Structure

- `sony_control.py`: importable Sony protocol, status parsing, power state, wake, and confirmed input selection.
- `sony-control`: thin command-line wrapper preserving the existing CLI.
- `wiim_client.py`: WiiM response model, strict JSON parsing, and bounded HTTP client.
- `wiim_sony.py`: configuration, transition policy, polling, logging suppression, and daemon assembly.
- `wiim-sony`: executable daemon entrypoint with `--once` diagnostic mode.
- `test_sony_control.py`: Sony module, safe power, and input behavior.
- `test_wiim_client.py`: WiiM response and HTTP client behavior.
- `test_wiim_sony.py`: configuration, transitions, policy, and polling recovery.
- `systemd/wiim-sony.service`: unprivileged service definition.
- `systemd/wiim-sony.env.example`: deployment configuration example.
- `docs/wiim-mini-api.md`: actual firmware observations, created during live validation.
- `README.md`: wiring, installation, operation, systemd, and troubleshooting.

### Task 1: Extract the Sony Controller into an Importable Module

**Files:**
- Create: `sony_control.py`
- Modify: `sony-control:1-317`
- Modify: `test_sony_control.py:1-61`

**Interfaces:**
- Produces: `SonyError`, `RegistrationRequired`, `Receiver`, `POWER_TOGGLE`, `FUNCTION_PLUS`, and `device_id()` from `sony_control`.
- Preserves: all existing `sony-control` subcommands and output.

- [ ] **Step 1: Change the test import to require the new module**

Replace the dynamic loader at the top of `test_sony_control.py` with:

```python
import unittest

import sony_control
```

- [ ] **Step 2: Run the test to verify the new module is missing**

Run: `python3 -m unittest -v test_sony_control.py`

Expected: ERROR with `ModuleNotFoundError: No module named 'sony_control'`.

- [ ] **Step 3: Move the reusable implementation into `sony_control.py`**

Move constants, errors, helpers, and `Receiver` from `sony-control` into
`sony_control.py`. Preserve `DEVICE_ID_FILE` beside the repository scripts:

```python
from pathlib import Path

DEVICE_ID_FILE = Path(__file__).resolve().with_name(".sony-control-device-id")
```

Keep `main()` and argument parsing in `sony-control`, importing the shared API:

```python
#!/usr/bin/env python3
import argparse
import os
import sys

from sony_control import Receiver, SonyError
```

- [ ] **Step 4: Run the existing tests and CLI checks**

Run: `python3 -m unittest -v test_sony_control.py`

Expected: all existing tests PASS.

Run: `./sony-control status && ./sony-control input SA-CD/CD`

Expected: live status succeeds and the second command reports SA-CD/CD with zero steps when it is already selected. If the Sony is currently on another input, run only `./sony-control status` and confirm the CLI parses the source without changing it.

- [ ] **Step 5: Commit the behavior-preserving extraction**

```bash
git add sony_control.py sony-control test_sony_control.py
git commit -m "Refactor Sony control into reusable module"
```

### Task 2: Add Safe Sony Power and Awake-Only Input Interfaces

**Files:**
- Modify: `sony_control.py`
- Modify: `sony-control`
- Modify: `test_sony_control.py`

**Interfaces:**
- Produces: `PowerState(Enum)` with `ON`, `STANDBY`, and `UNKNOWN`.
- Produces: `Receiver.power_state() -> PowerState`.
- Produces: `Receiver.wake_from_standby(wait: float) -> None`.
- Produces: `Receiver.select_input_when_awake(target: str, max_steps: int = 20) -> int`.
- Preserves: `Receiver.power_on()` and `Receiver.select_input()` for the manual CLI.

- [ ] **Step 1: Write failing tests for conservative power classification**

Add tests that make the production changes fail by name or behavior:

```python
class PowerStateTest(unittest.TestCase):
    def test_successful_active_probe_means_on(self):
        receiver = sony_control.Receiver("receiver")
        receiver.volume = lambda: 5
        self.assertEqual(receiver.power_state(), sony_control.PowerState.ON)

    def test_failed_active_probe_is_unknown(self):
        receiver = sony_control.Receiver("receiver")
        receiver.volume = lambda: (_ for _ in ()).throw(sony_control.SonyError("down"))
        self.assertEqual(receiver.power_state(), sony_control.PowerState.UNKNOWN)

    def test_awake_only_selection_never_calls_power_on(self):
        receiver = sony_control.Receiver("receiver")
        receiver.power_on = lambda: self.fail("must not call power_on")
        receiver.source = lambda: "SA-CD/CD"
        self.assertEqual(receiver.select_input_when_awake("SA-CD/CD"), 0)
```

- [ ] **Step 2: Run the tests and verify the interface is absent**

Run: `python3 -m unittest -v test_sony_control.PowerStateTest`

Expected: FAIL because `PowerState`, `power_state`, and
`select_input_when_awake` do not exist.

- [ ] **Step 3: Implement the safe interfaces**

Add the enum and conservative probe:

```python
from enum import Enum

class PowerState(Enum):
    ON = "on"
    STANDBY = "standby"
    UNKNOWN = "unknown"

def power_state(self) -> PowerState:
    try:
        self.volume()
    except (SonyError, ValueError):
        return PowerState.UNKNOWN
    return PowerState.ON
```

Extract the current source-cycling body into
`select_input_when_awake()`. Keep `select_input()` as the manual convenience
that calls `power_on()` first, then delegates.

Add an owned wake method that assumes its caller already established
`STANDBY`, sends exactly one toggle, and waits on the positive active probe:

```python
def wake_from_standby(self, wait: float = 20.0) -> None:
    self.send_ircc(POWER_TOGGLE)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if self.power_state() is PowerState.ON:
            return
    raise SonyError("Receiver did not become ready after the power command")
```

Do not make the production receiver return `STANDBY` yet. That state requires
the positive signature captured in Task 8.

- [ ] **Step 4: Add and pass the one-toggle wake test**

Use a fake monotonic clock or patch module `time` so the test does not sleep:

```python
def test_owned_wake_sends_one_toggle_and_waits_for_on(self):
    receiver = sony_control.Receiver("receiver")
    receiver.sent = []
    receiver.send_ircc = receiver.sent.append
    states = iter([sony_control.PowerState.UNKNOWN, sony_control.PowerState.ON])
    receiver.power_state = lambda: next(states)
    with unittest.mock.patch.object(sony_control.time, "sleep"):
        receiver.wake_from_standby(wait=2)
    self.assertEqual(receiver.sent, [sony_control.POWER_TOGGLE])
```

Run: `python3 -m unittest -v test_sony_control.py`

Expected: all Sony tests PASS.

- [ ] **Step 5: Commit the safe Sony API**

```bash
git add sony_control.py sony-control test_sony_control.py
git commit -m "Add conservative Sony power interfaces"
```

### Task 3: Implement Strict WiiM Status Parsing and HTTP Polling

**Files:**
- Create: `wiim_client.py`
- Create: `test_wiim_client.py`

**Interfaces:**
- Produces: `WiiMError`, `WiiMProtocolError`.
- Produces: immutable `WiiMStatus(raw_status: str, mode: str, airplay_mode: str)` with `playing` and `airplay_playing` properties.
- Produces: `parse_player_status(payload: bytes, airplay_mode: str = "1") -> WiiMStatus`.
- Produces: `WiiMClient(base_url: str, timeout: float, airplay_mode: str = "1", opener=urllib.request.urlopen)` and `fetch_status() -> WiiMStatus`.

- [ ] **Step 1: Write failing parser tests**

Create `test_wiim_client.py` with focused cases:

```python
import unittest

from wiim_client import WiiMProtocolError, parse_player_status

class PlayerStatusParsingTest(unittest.TestCase):
    def test_play_mode_one_is_actionable_airplay(self):
        status = parse_player_status(b'{"status":"play","mode":"1"}')
        self.assertTrue(status.playing)
        self.assertTrue(status.airplay_playing)

    def test_pause_stop_and_loading_are_not_playing(self):
        for value in ("pause", "stop", "loading", "load"):
            with self.subTest(value=value):
                status = parse_player_status(
                    f'{{"status":"{value}","mode":"1"}}'.encode()
                )
                self.assertFalse(status.playing)

    def test_mode_is_normalized_but_does_not_imply_playing(self):
        status = parse_player_status(b'{"status":"pause","mode":1}')
        self.assertEqual(status.mode, "1")
        self.assertFalse(status.airplay_playing)

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(WiiMProtocolError):
            parse_player_status(b'{"status":"buffering","mode":"1"}')

    def test_missing_or_non_object_payload_is_rejected(self):
        for payload in (b'{}', b'[]', b'not-json'):
            with self.subTest(payload=payload):
                with self.assertRaises(WiiMProtocolError):
                    parse_player_status(payload)
```

- [ ] **Step 2: Run the parser tests and verify they fail on import**

Run: `python3 -m unittest -v test_wiim_client.PlayerStatusParsingTest`

Expected: ERROR with `ModuleNotFoundError: No module named 'wiim_client'`.

- [ ] **Step 3: Implement the minimal response model and parser**

```python
import json
from dataclasses import dataclass

KNOWN_STATES = frozenset({"play", "pause", "stop", "loading", "load"})

class WiiMError(RuntimeError):
    pass

class WiiMProtocolError(WiiMError):
    pass

@dataclass(frozen=True)
class WiiMStatus:
    raw_status: str
    mode: str
    airplay_mode: str = "1"

    @property
    def playing(self) -> bool:
        return self.raw_status == "play"

    @property
    def airplay_playing(self) -> bool:
        return self.playing and self.mode == self.airplay_mode
```

`parse_player_status()` must decode UTF-8 JSON, require a dictionary, reject
boolean/container values for `status` and `mode`, normalize scalar values with
`str(value).strip().lower()`, and reject statuses outside `KNOWN_STATES`.

- [ ] **Step 4: Run the parser tests and verify they pass**

Run: `python3 -m unittest -v test_wiim_client.PlayerStatusParsingTest`

Expected: all parser tests PASS.

- [ ] **Step 5: Write failing HTTP client tests**

Use a context-manager fake response rather than opening a socket:

```python
class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.body

class WiiMClientTest(unittest.TestCase):
    def test_fetches_get_player_status_with_timeout(self):
        calls = []
        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse(b'{"status":"play","mode":"1"}')
        client = WiiMClient("http://wiim.local", 0.8, opener=opener)
        self.assertTrue(client.fetch_status().airplay_playing)
        self.assertEqual(
            calls,
            [("http://wiim.local/httpapi.asp?command=getPlayerStatus", 0.8)],
        )
```

Also assert that `URLError`, timeout, non-2xx responses, and invalid payloads
raise `WiiMError` without returning an idle state.

- [ ] **Step 6: Run the client tests and verify `WiiMClient` is absent**

Run: `python3 -m unittest -v test_wiim_client.WiiMClientTest`

Expected: FAIL because `WiiMClient` does not exist.

- [ ] **Step 7: Implement the bounded HTTP request**

Build the URL with `urllib.parse.urljoin`, send `Accept: application/json`, and
translate `HTTPError`, `URLError`, `TimeoutError`, and `OSError` to `WiiMError`.
Pass the response bytes to `parse_player_status()`.

- [ ] **Step 8: Run all WiiM client tests and commit**

Run: `python3 -m unittest -v test_wiim_client.py`

Expected: all WiiM client tests PASS.

```bash
git add wiim_client.py test_wiim_client.py
git commit -m "Add WiiM player status client"
```

### Task 4: Implement Configuration and Playback-Start Policy

**Files:**
- Create: `wiim_sony.py`
- Create: `test_wiim_sony.py`

**Interfaces:**
- Consumes: `WiiMStatus`, `Receiver`, and `PowerState`.
- Produces: immutable `Config` and `Config.from_env(env: Mapping[str, str])`.
- Produces: `AutomationController(sony, target_input, sony_ready_timeout, logger)` and `observe(status: WiiMStatus) -> None`.

- [ ] **Step 1: Write failing configuration tests**

```python
import unittest

from wiim_sony import Config, ConfigError

class ConfigTest(unittest.TestCase):
    def test_builds_http_url_from_wiim_ip(self):
        config = Config.from_env({"WIIM_IP": "wiim-test.example"})
        self.assertEqual(config.wiim_base_url, "http://wiim-test.example")
        self.assertEqual(config.sony_ip, "receiver.example")
        self.assertEqual(config.target_input, "SA-CD/CD")
        self.assertEqual(config.poll_interval, 1.5)

    def test_explicit_base_url_wins(self):
        config = Config.from_env({
            "WIIM_IP": "ignored",
            "WIIM_BASE_URL": "https://wiim-mini.local:443",
        })
        self.assertEqual(config.wiim_base_url, "https://wiim-mini.local:443")

    def test_missing_address_and_invalid_numbers_fail(self):
        with self.assertRaises(ConfigError):
            Config.from_env({})
        with self.assertRaises(ConfigError):
            Config.from_env({"WIIM_IP": "wiim", "POLL_INTERVAL": "0"})
```

- [ ] **Step 2: Run the configuration tests and verify the module is absent**

Run: `python3 -m unittest -v test_wiim_sony.ConfigTest`

Expected: ERROR with `ModuleNotFoundError: No module named 'wiim_sony'`.

- [ ] **Step 3: Implement validated configuration**

Define:

```python
@dataclass(frozen=True)
class Config:
    wiim_base_url: str
    wiim_airplay_mode: str
    sony_ip: str
    target_input: str
    poll_interval: float
    request_timeout: float
    sony_ready_timeout: float
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        base_url = env.get("WIIM_BASE_URL")
        if not base_url:
            wiim_ip = env.get("WIIM_IP")
            if not wiim_ip:
                raise ConfigError("WIIM_IP or WIIM_BASE_URL is required")
            base_url = f"http://{wiim_ip}"
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError("WIIM_BASE_URL must be an HTTP or HTTPS URL")
        log_level = env.get("LOG_LEVEL", "INFO").upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigError(f"Invalid LOG_LEVEL: {log_level}")
        return cls(
            wiim_base_url=base_url.rstrip("/"),
            wiim_airplay_mode=env.get("WIIM_AIRPLAY_MODE", "1"),
            sony_ip=env.get("SONY_IP", "receiver.example"),
            target_input=env.get("SONY_TARGET_INPUT", "SA-CD/CD"),
            poll_interval=positive_float(env, "POLL_INTERVAL", 1.5),
            request_timeout=positive_float(env, "REQUEST_TIMEOUT", 1.0),
            sony_ready_timeout=positive_float(env, "SONY_READY_TIMEOUT", 20.0),
            log_level=log_level,
        )
```

Require an `http` or `https` URL with a hostname. Validate all time values as
finite and greater than zero. Validate `LOG_LEVEL` through
`logging.getLevelNamesMapping()`.

Define the numeric helper used above:

```python
def positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value
```

- [ ] **Step 4: Run configuration tests and verify they pass**

Run: `python3 -m unittest -v test_wiim_sony.ConfigTest`

Expected: all configuration tests PASS.

- [ ] **Step 5: Write failing transition and policy tests with a fake Sony**

```python
class FakeSony:
    def __init__(self, states):
        self.states = iter(states)
        self.calls = []
    def power_state(self):
        self.calls.append("power_state")
        return next(self.states)
    def wake_from_standby(self, wait):
        self.calls.append(("wake", wait))
    def select_input_when_awake(self, target):
        self.calls.append(("input", target))
        return 1
    def source(self):
        return "SA-CD/CD"
```

Cover the transition and policy branches with concrete tests:

```python
class AutomationControllerTest(unittest.TestCase):
    def controller(self, sony):
        return AutomationController(
            sony, "SA-CD/CD", 20.0, logging.getLogger("test.automation")
        )

    def status(self, raw_status="play", mode="1"):
        return WiiMStatus(raw_status, mode, "1")

    def test_first_airplay_play_is_actionable(self):
        sony = FakeSony([PowerState.STANDBY])
        self.controller(sony).observe(self.status())
        self.assertEqual(
            sony.calls,
            ["power_state", ("wake", 20.0), ("input", "SA-CD/CD")],
        )

    def test_repeated_play_does_not_repeat_actions(self):
        sony = FakeSony([PowerState.ON])
        controller = self.controller(sony)
        controller.observe(self.status())
        controller.observe(self.status())
        self.assertEqual(sony.calls, ["power_state"])

    def test_pause_then_play_creates_second_transition(self):
        sony = FakeSony([PowerState.ON, PowerState.ON])
        controller = self.controller(sony)
        controller.observe(self.status())
        controller.observe(self.status("pause"))
        controller.observe(self.status())
        self.assertEqual(sony.calls, ["power_state", "power_state"])

    def test_non_airplay_play_is_not_actionable(self):
        sony = FakeSony([])
        self.controller(sony).observe(self.status(mode="10"))
        self.assertEqual(sony.calls, [])

    def test_sony_on_is_never_switched(self):
        sony = FakeSony([PowerState.ON])
        self.controller(sony).observe(self.status())
        self.assertEqual(sony.calls, ["power_state"])

    def test_sony_unknown_receives_no_command(self):
        sony = FakeSony([PowerState.UNKNOWN])
        self.controller(sony).observe(self.status())
        self.assertEqual(sony.calls, ["power_state"])
```

- [ ] **Step 6: Run policy tests and verify `AutomationController` is absent**

Run: `python3 -m unittest -v test_wiim_sony.AutomationControllerTest`

Expected: FAIL because `AutomationController` does not exist.

- [ ] **Step 7: Implement the in-memory transition controller**

Store `_previous_playing: bool | None`. Compute current actionability from
`status.airplay_playing`. On the first `True` or a `False -> True` transition,
call `_handle_playback_started()` once. Update state before performing Sony I/O
so an exception cannot create a command loop on the next poll.

The handler must branch exactly on `PowerState`:

```python
state = self.sony.power_state()
if state is PowerState.ON:
    return
if state is PowerState.UNKNOWN:
    return
self.sony.wake_from_standby(self.sony_ready_timeout)
self.sony.select_input_when_awake(self.target_input)
confirmed = self.sony.source()
if confirmed.casefold() != self.target_input.casefold():
    raise SonyError(f"Sony input confirmation failed: {confirmed}")
```

Log the transition, classification, wake, readiness, selection, and confirmed
source without logging steady successful observations.

- [ ] **Step 8: Run policy tests and commit**

Run: `python3 -m unittest -v test_wiim_sony.py`

Expected: all configuration and policy tests PASS.

```bash
git add wiim_sony.py test_wiim_sony.py
git commit -m "Add WiiM playback transition policy"
```

### Task 5: Add a Resilient Polling Service

**Files:**
- Modify: `wiim_sony.py`
- Modify: `test_wiim_sony.py`

**Interfaces:**
- Consumes: an object with `fetch_status() -> WiiMStatus` and an `AutomationController`.
- Produces: `PollingService.poll_once() -> None` and `run_forever(stop_event: threading.Event) -> None`.

- [ ] **Step 1: Write failing polling recovery tests**

Use a sequence client whose results are status objects or exceptions:

```python
class SequenceClient:
    def __init__(self, results):
        self.results = iter(results)
    def fetch_status(self):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result
```

Use concrete recovery and state-preservation tests:

```python
class RecordingController:
    def __init__(self):
        self.statuses = []
    def observe(self, status):
        self.statuses.append(status)

class PollingServiceTest(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.polling")
        self.playing = WiiMStatus("play", "1", "1")

    def test_logs_one_failure_then_recovery(self):
        controller = RecordingController()
        service = PollingService(
            SequenceClient([WiiMError("down"), WiiMError("down"), self.playing]),
            controller,
            1.5,
            self.logger,
        )
        with self.assertLogs("test.polling", level="INFO") as logs:
            service.poll_once()
            service.poll_once()
            service.poll_once()
        self.assertEqual(controller.statuses, [self.playing])
        self.assertEqual(sum("unreachable" in line for line in logs.output), 1)
        self.assertEqual(sum("recovered" in line for line in logs.output), 1)

    def test_outage_does_not_create_a_second_play_transition(self):
        sony = FakeSony([PowerState.ON])
        controller = AutomationController(
            sony, "SA-CD/CD", 20.0, logging.getLogger("test.automation")
        )
        service = PollingService(
            SequenceClient([self.playing, WiiMError("down"), self.playing]),
            controller,
            1.5,
            self.logger,
        )
        service.poll_once()
        service.poll_once()
        service.poll_once()
        self.assertEqual(sony.calls, ["power_state"])

    def test_unexpected_exception_is_contained(self):
        service = PollingService(
            SequenceClient([RuntimeError("boom")]),
            RecordingController(),
            1.5,
            self.logger,
        )
        with self.assertLogs("test.polling", level="ERROR"):
            service.poll_once()
```

- [ ] **Step 2: Run the polling tests and verify `PollingService` is absent**

Run: `python3 -m unittest -v test_wiim_sony.PollingServiceTest`

Expected: FAIL because `PollingService` does not exist.

- [ ] **Step 3: Implement failure containment and recovery logging**

`poll_once()` tracks `_wiim_unavailable`. Catch `WiiMError` separately from
unexpected exceptions. Set the flag and warn only on the first consecutive
failure. On success after failure, log recovery, clear the flag, and call
`controller.observe(status)`.

`run_forever()` uses a supplied `threading.Event` and waits the configured
interval after each completed poll:

```python
def run_forever(self, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        self.poll_once()
        stop_event.wait(self.poll_interval)
```

- [ ] **Step 4: Run the full test suite and commit**

Run: `python3 -m unittest -v`

Expected: all tests PASS with no network access.

```bash
git add wiim_sony.py test_wiim_sony.py
git commit -m "Add resilient WiiM polling service"
```

### Task 6: Add the Daemon Entrypoint and Manual Diagnostic Mode

**Files:**
- Create: `wiim-sony`
- Modify: `test_wiim_sony.py`

**Interfaces:**
- Produces: executable `wiim-sony` with normal continuous operation and `--once`.
- Consumes: `Config`, `WiiMClient`, `Receiver`, `AutomationController`, and `PollingService`.

- [ ] **Step 1: Write a failing entrypoint assembly test**

Expose `build_service(config: Config) -> PollingService` from `wiim_sony.py` and
test assembly and one-shot execution without opening sockets:

```python
class EntrypointTest(unittest.TestCase):
    def test_build_service_passes_configuration_to_components(self):
        config = Config.from_env({
            "WIIM_BASE_URL": "http://wiim.local",
            "SONY_IP": "receiver.example",
            "REQUEST_TIMEOUT": "0.8",
            "SONY_READY_TIMEOUT": "12",
        })
        with (
            unittest.mock.patch.object(wiim_sony, "WiiMClient") as wiim_class,
            unittest.mock.patch.object(wiim_sony, "Receiver") as sony_class,
            unittest.mock.patch.object(
                wiim_sony, "AutomationController"
            ) as controller_class,
            unittest.mock.patch.object(wiim_sony, "PollingService") as service_class,
        ):
            service = build_service(config)
        wiim_class.assert_called_once_with("http://wiim.local", 0.8, "1")
        sony_class.assert_called_once_with("receiver.example", timeout=0.8)
        controller_class.assert_called_once_with(
            sony_class.return_value,
            "SA-CD/CD",
            12.0,
            unittest.mock.ANY,
        )
        self.assertIs(service, service_class.return_value)

    def test_once_polls_once_and_exits_zero(self):
        service = unittest.mock.Mock()
        with unittest.mock.patch.object(
            wiim_sony, "build_service", return_value=service
        ):
            result = main(["--once"], {"WIIM_IP": "wiim-mini.local"})
        self.assertEqual(result, 0)
        service.poll_once.assert_called_once_with()
        service.run_forever.assert_not_called()

    def test_invalid_configuration_exits_nonzero(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(["--once"], {})
        self.assertNotEqual(result, 0)
        self.assertIn("WIIM_IP or WIIM_BASE_URL is required", stderr.getvalue())
```

- [ ] **Step 2: Run the entrypoint tests and verify assembly is absent**

Run: `python3 -m unittest -v test_wiim_sony.EntrypointTest`

Expected: FAIL because `build_service` and `main` do not exist.

- [ ] **Step 3: Implement assembly and the thin executable**

`build_service()` constructs:

```python
wiim = WiiMClient(
    config.wiim_base_url,
    config.request_timeout,
    config.wiim_airplay_mode,
)
sony = Receiver(config.sony_ip, timeout=config.request_timeout)
logger = logging.getLogger("wiim-sony")
controller = AutomationController(
    sony,
    config.target_input,
    config.sony_ready_timeout,
    logger,
)
return PollingService(wiim, controller, config.poll_interval, logger)
```

The executable imports and exits through `wiim_sony.main()`:

```python
#!/usr/bin/env python3
from wiim_sony import main

raise SystemExit(main())
```

Normal mode creates a `threading.Event`, installs SIGINT/SIGTERM handlers that
set it, and calls `run_forever()`. `--once` performs one poll for installation
diagnostics.

- [ ] **Step 4: Run tests, syntax checks, and help output**

Run: `python3 -m unittest -v`

Run: `python3 -m py_compile sony_control.py wiim_client.py wiim_sony.py sony-control wiim-sony`

Run: `./wiim-sony --help`

Expected: tests and compilation PASS; help documents `--once`.

- [ ] **Step 5: Commit the executable daemon**

```bash
git add wiim_sony.py wiim-sony test_wiim_sony.py
git commit -m "Add WiiM Sony daemon entrypoint"
```

### Task 7: Add systemd Packaging and Installation Documentation

**Files:**
- Create: `systemd/wiim-sony.service`
- Create: `systemd/wiim-sony.env.example`
- Modify: `README.md`

**Interfaces:**
- Produces: an unprivileged service that starts after network readiness and logs to journald.
- Documents: manual operation and the live-validation gate.

- [ ] **Step 1: Create the systemd unit**

Use this exact unit:

```ini
[Unit]
Description=WiiM playback to Sony STR-DN840 input automation
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=SERVICE_USER
WorkingDirectory=/path/to/Sony-STR-DN840-Remote-Python
EnvironmentFile=/etc/wiim-sony.env
ExecStart=/usr/bin/python3 /path/to/Sony-STR-DN840-Remote-Python/wiim-sony
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create the environment example**

```dotenv
WIIM_IP=wiim-mini.local
SONY_IP=receiver.example
SONY_TARGET_INPUT=SA-CD/CD
POLL_INTERVAL=1.5
REQUEST_TIMEOUT=1.0
SONY_READY_TIMEOUT=20
WIIM_AIRPLAY_MODE=1
LOG_LEVEL=INFO
```

- [ ] **Step 3: Expand the README with current-state documentation**

Document:

- AirPlay -> WiiM Mini -> RCA -> Sony SA-CD/CD wiring;
- WiiM and Sony LAN control paths through `automation-host.example`;
- Network Standby and CERS registration prerequisites;
- official WiiM expectations (`status=play`, AirPlay mode `1`) clearly marked
  as awaiting installed-firmware confirmation;
- every environment variable and `WIIM_BASE_URL` override;
- `./wiim-sony --once` and normal foreground operation;
- service installation commands, with a warning not to enable yet;
- `systemctl status wiim-sony` and `journalctl -u wiim-sony -f`;
- troubleshooting for 403 registration, WiiM unreachable, malformed status,
  Sony `UNKNOWN`, wake timeout, and input confirmation failure;
- changing `SONY_TARGET_INPUT`;
- no automatic power-off and no switching when the Sony is already on.

Do not describe future work as current behavior and do not embed migration or
commit history.

- [ ] **Step 4: Verify packaging without installing it**

Run: `systemd-analyze verify systemd/wiim-sony.service`

Run: `env WIIM_IP=localhost ./wiim-sony --once`

Expected: unit verification succeeds. The one-shot command logs a bounded WiiM
connection failure and exits without a traceback or Sony command.

Run: `python3 -m unittest -v && git diff --check`

Expected: all tests PASS and no whitespace errors are reported.

- [ ] **Step 5: Commit packaging and documentation**

```bash
git add systemd/wiim-sony.service systemd/wiim-sony.env.example README.md
git commit -m "Document and package WiiM Sony service"
```

### Task 8: Characterize the Installed Devices and Enable Automation

**Files:**
- Create: `docs/wiim-mini-api.md`
- Create as observed: `tests/fixtures/wiim-mini/*.json`
- Modify if observations require it: `wiim_client.py`, `test_wiim_client.py`, `sony_control.py`, `test_sony_control.py`, `README.md`, `systemd/wiim-sony.env.example`
- Install after all checks pass: `/etc/systemd/system/wiim-sony.service`, `/etc/wiim-sony.env`

**Interfaces:**
- Consumes: the installed WiiM Mini address supplied as `WIIM_IP`.
- Produces: actual response fixtures, a validated Sony standby signature, passed live scenarios, and an enabled service.

- [ ] **Step 1: Establish the WiiM address and transport**

Set `WIIM_IP` from the router's DHCP lease or the WiiM app, then test the handoff
endpoint:

```bash
export WIIM_IP
curl --fail --silent --show-error \
  "http://${WIIM_IP}/httpapi.asp?command=getPlayerStatus"
```

If HTTP fails while the host is reachable, probe documented HTTPS explicitly:

```bash
curl --insecure --fail --silent --show-error \
  "https://${WIIM_IP}/httpapi.asp?command=getPlayerStatus"
```

Set `WIIM_BASE_URL` to the transport that returns valid JSON. Do not disable TLS
verification in daemon code until the exact certificate behavior is captured
and a test describes the chosen behavior.

- [ ] **Step 2: Capture actual Mini status fixtures**

For each state, save the unmodified `getPlayerStatus` body under
`tests/fixtures/wiim-mini/` using these names:

```text
idle.json
airplay-connected-silent.json
airplay-playing.json
airplay-paused.json
airplay-resumed.json
airplay-disconnected.json
```

Also query and record whether these commands exist, without making the daemon
depend on them:

```bash
curl --fail --silent --show-error \
  "http://${WIIM_IP}/httpapi.asp?command=getStatusEx"
curl --fail --silent --show-error \
  "http://${WIIM_IP}/httpapi.asp?command=getPlayerStatusEx"
```

- [ ] **Step 3: Turn every parser discrepancy into a failing test first**

Load each fixture in `test_wiim_client.py` and assert the observed status,
mode, playing flag, and AirPlay flag. If key casing, values, or transport differ
from the offline assumptions, run the new tests and observe the expected
failure before changing production code.

Run: `python3 -m unittest -v test_wiim_client.py`

Expected: fixtures matching the existing parser PASS; any real discrepancy
fails with a specific parser assertion before the implementation is adjusted.

- [ ] **Step 4: Characterize Sony on and standby without guessing**

Coordinate a period when TV audio is not in use. Capture these positive probes
while the Sony is on, then have the operator put it into standby manually and
repeat them:

```bash
curl --max-time 3 --silent --show-error \
  "http://receiver.example:8080/description.xml"
curl --max-time 3 --silent --show-error \
  "http://receiver.example:50001/cers/getSystemInformation"
./sony-control status
```

The standby classifier may be implemented only if the receiver returns a
repeatable positive network response in standby that is distinguishable from
the positive active UPnP response. Require the signature on three consecutive
probes. If the receiver is simply unreachable or results conflict, preserve
`PowerState.UNKNOWN`, do not enable automatic wake, and document the blocker.

- [ ] **Step 5: Implement the observed standby signature through TDD**

Add fixtures or fake request sequences to `test_sony_control.py` that exactly
match the captured on, standby, and unreachable responses. First verify the
standby test fails because it returns `UNKNOWN`; then implement only the
validated signature so:

```python
on_probe -> PowerState.ON
validated_standby_probe -> PowerState.STANDBY
unreachable_or_conflicting_probe -> PowerState.UNKNOWN
```

Run: `python3 -m unittest -v test_sony_control.py`

Expected: all three classifications PASS.

- [ ] **Step 6: Run all seven live scenarios manually**

Run the daemon in the foreground with `LOG_LEVEL=INFO`. Verify each scenario
from the design spec, including startup during active playback, Sony already on
TV, Sony already on SA-CD/CD, both devices temporarily unavailable, and
play-pause-play. Confirm the log contains transitions and actions without one
line per successful poll.

Do not proceed if any power toggle occurs from `UNKNOWN`, the TV source is
stolen while Sony is already on, input commands repeat during steady playback,
or a transient failure terminates the daemon.

- [ ] **Step 7: Document actual firmware behavior**

Create `docs/wiim-mini-api.md` with the installed model, firmware version,
working transport and port, actual relevant fields, observed values for every
captured state, mode behavior across pause/disconnect, and unsupported
endpoints. Update README claims so installed-device observations are stated as
facts and remaining limitations are explicit.

- [ ] **Step 8: Run final verification and commit live findings**

Run:

```bash
python3 -m unittest -v
python3 -m py_compile sony_control.py wiim_client.py wiim_sony.py sony-control wiim-sony
systemd-analyze verify systemd/wiim-sony.service
git diff --check
```

Expected: every command exits zero.

```bash
git add docs/wiim-mini-api.md tests/fixtures/wiim-mini \
  wiim_client.py test_wiim_client.py sony_control.py test_sony_control.py \
  README.md systemd/wiim-sony.env.example
git commit -m "Validate WiiM Mini and Sony automation"
```

- [ ] **Step 9: Install and enable the service only after validation**

```bash
sudo install -m 0644 systemd/wiim-sony.service /etc/systemd/system/wiim-sony.service
sudo install -m 0600 systemd/wiim-sony.env.example /etc/wiim-sony.env
sudoedit /etc/wiim-sony.env
sudo systemctl daemon-reload
sudo systemctl enable --now wiim-sony
systemctl status wiim-sony
journalctl -u wiim-sony -n 50 --no-pager
```

Confirm the service runs as `SERVICE_USER`, uses the actual WiiM address, remains
healthy through a temporary WiiM outage, and leaves the Sony on SA-CD/CD after
the validated standby-to-AirPlay scenario.
