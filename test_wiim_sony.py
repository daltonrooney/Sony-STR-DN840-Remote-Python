import logging
import math
import unittest
from dataclasses import FrozenInstanceError
from enum import Enum

from sony_control import PowerState, SonyError
from wiim_client import WiiMStatus
from wiim_sony import AutomationController, Config, ConfigError


class FakeSony:
    def __init__(self, states, confirmed_source="SA-CD/CD"):
        self.states = iter(states)
        self.confirmed_source = confirmed_source
        self.calls = []
        self.source_calls = 0

    def power_state(self):
        self.calls.append("power_state")
        return next(self.states)

    def wake_from_standby(self, wait):
        self.calls.append(("wake", wait))

    def select_input_when_awake(self, target):
        self.calls.append(("input", target))
        return 1

    def source(self):
        self.source_calls += 1
        return self.confirmed_source


class ConfigTest(unittest.TestCase):
    def test_builds_http_url_from_wiim_ip(self):
        config = Config.from_env({"WIIM_IP": "wiim-test.example"})

        self.assertEqual(config.wiim_base_url, "http://wiim-test.example")
        self.assertEqual(config.wiim_airplay_mode, "1")
        self.assertEqual(config.sony_ip, "receiver.example")
        self.assertEqual(config.target_input, "SA-CD/CD")
        self.assertEqual(config.poll_interval, 1.5)
        self.assertEqual(config.request_timeout, 1.0)
        self.assertEqual(config.sony_ready_timeout, 20.0)
        self.assertEqual(config.log_level, "INFO")

    def test_explicit_base_url_wins_and_trailing_slash_is_removed(self):
        config = Config.from_env(
            {
                "WIIM_IP": "ignored",
                "WIIM_BASE_URL": "https://wiim-mini.local:443/",
            }
        )

        self.assertEqual(config.wiim_base_url, "https://wiim-mini.local:443")

    def test_missing_address_fails(self):
        with self.assertRaises(ConfigError):
            Config.from_env({})

    def test_malformed_base_urls_fail(self):
        for base_url in (
            "wiim.local",
            "ftp://wiim.local",
            "http:///status",
            "http://",
            "http://wiim.local:invalid",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ConfigError):
                    Config.from_env({"WIIM_BASE_URL": base_url})

    def test_invalid_time_values_fail(self):
        for name in ("POLL_INTERVAL", "REQUEST_TIMEOUT", "SONY_READY_TIMEOUT"):
            for value in ("0", "-1", "not-a-number", str(math.inf), str(math.nan)):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigError):
                        Config.from_env({"WIIM_IP": "wiim", name: value})

    def test_invalid_log_level_fails(self):
        with self.assertRaises(ConfigError):
            Config.from_env({"WIIM_IP": "wiim", "LOG_LEVEL": "verbose"})

    def test_overrides_are_normalized_and_preserved(self):
        config = Config.from_env(
            {
                "WIIM_IP": "wiim",
                "WIIM_AIRPLAY_MODE": "2",
                "SONY_IP": "sony.local",
                "SONY_TARGET_INPUT": "Video 1",
                "POLL_INTERVAL": "2.5",
                "REQUEST_TIMEOUT": "3",
                "SONY_READY_TIMEOUT": "12.25",
                "LOG_LEVEL": "debug",
            }
        )

        self.assertEqual(config.wiim_airplay_mode, "2")
        self.assertEqual(config.sony_ip, "sony.local")
        self.assertEqual(config.target_input, "Video 1")
        self.assertEqual(config.poll_interval, 2.5)
        self.assertEqual(config.request_timeout, 3.0)
        self.assertEqual(config.sony_ready_timeout, 12.25)
        self.assertEqual(config.log_level, "DEBUG")

    def test_config_is_immutable(self):
        config = Config.from_env({"WIIM_IP": "wiim"})

        with self.assertRaises(FrozenInstanceError):
            config.sony_ip = "other"


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
        self.assertEqual(sony.source_calls, 1)

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
        self.assertEqual(sony.source_calls, 0)

    def test_sony_unknown_receives_no_command(self):
        sony = FakeSony([PowerState.UNKNOWN])

        self.controller(sony).observe(self.status())

        self.assertEqual(sony.calls, ["power_state"])
        self.assertEqual(sony.source_calls, 0)

    def test_only_standby_state_may_wake(self):
        class FuturePowerState(Enum):
            OFF = "off"

        sony = FakeSony([FuturePowerState.OFF])

        self.controller(sony).observe(self.status())

        self.assertEqual(sony.calls, ["power_state"])
        self.assertEqual(sony.source_calls, 0)

    def test_state_is_updated_before_sony_io_failure(self):
        class FailingSony(FakeSony):
            def power_state(self):
                self.calls.append("power_state")
                raise SonyError("offline")

        sony = FailingSony([])
        controller = self.controller(sony)

        with self.assertRaises(SonyError):
            controller.observe(self.status())
        controller.observe(self.status())

        self.assertEqual(sony.calls, ["power_state"])

    def test_source_confirmation_is_case_insensitive(self):
        sony = FakeSony([PowerState.STANDBY], confirmed_source="sa-cd/cd")

        self.controller(sony).observe(self.status())

        self.assertEqual(sony.source_calls, 1)

    def test_source_confirmation_failure_raises(self):
        sony = FakeSony([PowerState.STANDBY], confirmed_source="TV")

        with self.assertRaisesRegex(SonyError, "Sony input confirmation failed: TV"):
            self.controller(sony).observe(self.status())

        self.assertEqual(sony.source_calls, 1)

    def test_transition_steps_are_logged_but_steady_play_is_not(self):
        sony = FakeSony([PowerState.STANDBY])
        controller = self.controller(sony)

        with self.assertLogs("test.automation", level="INFO") as captured:
            controller.observe(self.status())
        with self.assertNoLogs("test.automation", level="INFO"):
            controller.observe(self.status())

        messages = "\n".join(captured.output)
        for expected in (
            "playback started",
            "STANDBY",
            "waking",
            "ready",
            "selecting input SA-CD/CD",
            "confirmed source SA-CD/CD",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, messages)


if __name__ == "__main__":
    unittest.main()
