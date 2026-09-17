import contextlib
import io
import logging
import math
import signal
import unittest
from dataclasses import FrozenInstanceError
from enum import Enum
from unittest import mock

from sony_control import PowerState, SonyError
from wiim_client import WiiMError, WiiMStatus
import wiim_sony
from wiim_sony import AutomationController, Config, ConfigError, PollingService


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


class SequenceClient:
    def __init__(self, results):
        self.results = iter(results)

    def fetch_status(self):
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


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

    def test_unexpected_outage_logs_once_then_recovers(self):
        controller = RecordingController()
        service = PollingService(
            SequenceClient([RuntimeError("boom"), RuntimeError("boom"), self.playing]),
            controller,
            1.5,
            self.logger,
        )

        with self.assertLogs("test.polling", level="INFO") as logs:
            service.poll_once()
            service.poll_once()
            service.poll_once()

        self.assertEqual(controller.statuses, [self.playing])
        self.assertEqual(
            sum("Unexpected error polling WiiM" in line for line in logs.output), 1
        )
        self.assertEqual(sum("recovered" in line for line in logs.output), 1)

    def test_sony_failure_is_contained_without_steady_play_retry(self):
        sony = FakeSony([PowerState.STANDBY], confirmed_source="TV")
        controller = AutomationController(
            sony, "SA-CD/CD", 20.0, logging.getLogger("test.automation")
        )
        service = PollingService(
            SequenceClient([self.playing, self.playing]),
            controller,
            1.5,
            self.logger,
        )

        with self.assertLogs("test.polling", level="INFO") as logs:
            service.poll_once()
            service.poll_once()

        self.assertEqual(
            sony.calls,
            ["power_state", ("wake", 20.0), ("input", "SA-CD/CD")],
        )
        self.assertEqual(sony.source_calls, 1)
        self.assertEqual(
            sum("Sony controller action failed" in line for line in logs.output), 1
        )
        self.assertEqual(sum("recovered" in line for line in logs.output), 0)

    def test_pause_then_play_retries_failed_controller_action(self):
        sony = FakeSony([PowerState.STANDBY, PowerState.STANDBY], confirmed_source="TV")
        controller = AutomationController(
            sony, "SA-CD/CD", 20.0, logging.getLogger("test.automation")
        )
        service = PollingService(
            SequenceClient([self.playing, WiiMStatus("pause", "1", "1"), self.playing]),
            controller,
            1.5,
            self.logger,
        )

        with self.assertLogs("test.polling", level="ERROR") as logs:
            service.poll_once()
            service.poll_once()
            service.poll_once()

        self.assertEqual(
            sony.calls,
            [
                "power_state",
                ("wake", 20.0),
                ("input", "SA-CD/CD"),
                "power_state",
                ("wake", 20.0),
                ("input", "SA-CD/CD"),
            ],
        )
        self.assertEqual(sony.source_calls, 2)
        self.assertEqual(
            sum("Sony controller action failed" in line for line in logs.output), 2
        )

    def test_unexpected_controller_error_is_contained(self):
        class FailingController:
            def observe(self, status):
                raise RuntimeError("controller boom")

        service = PollingService(
            SequenceClient([self.playing]),
            FailingController(),
            1.5,
            self.logger,
        )

        with self.assertLogs("test.polling", level="ERROR") as logs:
            service.poll_once()

        self.assertEqual(
            sum("Unexpected controller error" in line for line in logs.output), 1
        )

    def test_waits_on_stop_event_between_polls(self):
        class StopAfterWait:
            def __init__(self):
                self.stopped = False
                self.intervals = []

            def is_set(self):
                return self.stopped

            def wait(self, interval):
                self.intervals.append(interval)
                self.stopped = True

        stop_event = StopAfterWait()
        service = PollingService(
            SequenceClient([self.playing]),
            RecordingController(),
            1.5,
            self.logger,
        )

        service.run_forever(stop_event)

        self.assertEqual(stop_event.intervals, [1.5])


class EntrypointTest(unittest.TestCase):
    def test_build_service_passes_configuration_to_components(self):
        config = Config.from_env(
            {
                "WIIM_BASE_URL": "http://wiim.local",
                "SONY_IP": "receiver.example",
                "REQUEST_TIMEOUT": "0.8",
                "SONY_READY_TIMEOUT": "12",
            }
        )

        with (
            mock.patch.object(wiim_sony, "WiiMClient") as wiim_class,
            mock.patch.object(wiim_sony, "Receiver") as sony_class,
            mock.patch.object(wiim_sony, "AutomationController") as controller_class,
            mock.patch.object(wiim_sony, "PollingService") as service_class,
        ):
            service = wiim_sony.build_service(config)

        wiim_class.assert_called_once_with("http://wiim.local", 0.8, "1")
        sony_class.assert_called_once_with("receiver.example", timeout=0.8)
        controller_class.assert_called_once_with(
            sony_class.return_value,
            "SA-CD/CD",
            12.0,
            mock.ANY,
        )
        self.assertIs(service, service_class.return_value)

    def test_once_polls_once_and_exits_zero(self):
        service = mock.Mock()

        with mock.patch.object(wiim_sony, "build_service", return_value=service):
            result = wiim_sony.main(["--once"], {"WIIM_IP": "wiim-mini.local"})

        self.assertEqual(result, 0)
        service.poll_once.assert_called_once_with()
        service.run_forever.assert_not_called()

    def test_invalid_configuration_exits_nonzero(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            result = wiim_sony.main(["--once"], {})

        self.assertNotEqual(result, 0)
        self.assertIn("WIIM_IP or WIIM_BASE_URL is required", stderr.getvalue())

    def test_normal_mode_runs_until_a_signal_sets_the_stop_event(self):
        service = mock.Mock()
        handlers = {}

        with (
            mock.patch.object(wiim_sony, "build_service", return_value=service),
            mock.patch.object(
                wiim_sony.signal,
                "signal",
                side_effect=lambda signal_number, handler: handlers.setdefault(
                    signal_number, handler
                ),
            ),
        ):
            result = wiim_sony.main([], {"WIIM_IP": "wiim-mini.local"})

        self.assertEqual(result, 0)
        stop_event = service.run_forever.call_args.args[0]
        self.assertFalse(stop_event.is_set())
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.assertTrue(stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
