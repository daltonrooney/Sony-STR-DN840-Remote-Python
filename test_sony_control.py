import contextlib
import io
import os
import runpy
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import sony_control


class ParseStatusTest(unittest.TestCase):
    def test_extracts_source_and_power(self):
        receiver = sony_control.Receiver("receiver")
        receiver._request = lambda *args, **kwargs: (
            200,
            '<root><status name="power" value="on"/>'
            '<status name="viewing"><item field="source" value="SA-CD/CD"/>'
            '<item field="title" value=""/></status></root>',
        )

        self.assertEqual(
            receiver.cers_status(), {"power": "on", "source": "SA-CD/CD", "title": ""}
        )

    def test_requires_registration(self):
        receiver = sony_control.Receiver("receiver")
        receiver._request = lambda *args, **kwargs: (403, "")

        with self.assertRaises(sony_control.RegistrationRequired):
            receiver.cers_status()


class PowerStateTest(unittest.TestCase):
    def test_disabled_standby_source_is_unknown(self):
        receiver = sony_control.Receiver("receiver")
        receiver.source = lambda: self.fail("must not probe without a sentinel")

        self.assertEqual(receiver.power_state(), sony_control.PowerState.UNKNOWN)

    def test_failed_source_probe_is_unknown(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.source = lambda: (_ for _ in ()).throw(sony_control.SonyError("down"))

        self.assertEqual(receiver.power_state(), sony_control.PowerState.UNKNOWN)

    def test_matching_source_means_standby(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.source = lambda: "BD"

        self.assertEqual(receiver.power_state(), sony_control.PowerState.STANDBY)

    def test_malformed_cers_status_means_unknown(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver._request = lambda *args, **kwargs: (200, "<not-xml")

        self.assertEqual(receiver.power_state(), sony_control.PowerState.UNKNOWN)

    def test_unwrapped_xml_parse_failure_means_unknown(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.source = lambda: (_ for _ in ()).throw(ET.ParseError("bad XML"))

        self.assertEqual(receiver.power_state(), sony_control.PowerState.UNKNOWN)

    def test_matching_source_is_case_insensitive(self):
        receiver = sony_control.Receiver("receiver", standby_source="bd")
        receiver.source = lambda: "BD"

        self.assertEqual(receiver.power_state(), sony_control.PowerState.STANDBY)

    def test_other_known_source_means_on(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.source = lambda: "SA-CD/CD"

        self.assertEqual(receiver.power_state(), sony_control.PowerState.ON)

    def test_power_on_refuses_unknown_state_without_sending_toggle(self):
        receiver = sony_control.Receiver("receiver")
        receiver.sent = []
        receiver.send_ircc = receiver.sent.append

        with self.assertRaisesRegex(
            sony_control.SonyError, "power state is unknown"
        ):
            receiver.power_on()

        self.assertEqual(receiver.sent, [])

    def test_awake_only_selection_never_calls_power_on(self):
        receiver = sony_control.Receiver("receiver")
        receiver.power_on = lambda: self.fail("must not call power_on")
        receiver.source = lambda: "SA-CD/CD"

        self.assertEqual(receiver.select_input_when_awake("SA-CD/CD"), 0)

    def test_wake_times_out_after_one_toggle_while_source_stays_standby(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.sent = []
        receiver.send_ircc = receiver.sent.append
        receiver.source = mock.Mock(return_value="BD")

        with (
            mock.patch.object(sony_control.time, "sleep"),
            mock.patch.object(
                sony_control.time, "monotonic", side_effect=[0.0, 0.0, 0.5, 1.0]
            ),
            self.assertRaisesRegex(sony_control.SonyError, "did not become ready"),
        ):
            receiver.wake_from_standby(wait=1.0)

        self.assertEqual(receiver.sent, [sony_control.POWER_TOGGLE])
        self.assertEqual(receiver.source.call_count, 2)

    def test_wake_times_out_after_one_toggle_while_state_stays_unknown(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.sent = []
        receiver.send_ircc = receiver.sent.append
        receiver.source = mock.Mock(side_effect=sony_control.SonyError("down"))

        with (
            mock.patch.object(sony_control.time, "sleep"),
            mock.patch.object(
                sony_control.time, "monotonic", side_effect=[0.0, 0.0, 0.5, 1.0]
            ),
            self.assertRaisesRegex(sony_control.SonyError, "did not become ready"),
        ):
            receiver.wake_from_standby(wait=1.0)

        self.assertEqual(receiver.sent, [sony_control.POWER_TOGGLE])
        self.assertEqual(receiver.source.call_count, 2)

    def test_wake_tolerates_transient_unknown_without_another_toggle(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.sent = []
        receiver.send_ircc = receiver.sent.append
        receiver.power_state = mock.Mock(
            side_effect=[sony_control.PowerState.UNKNOWN, sony_control.PowerState.ON]
        )

        with (
            mock.patch.object(sony_control.time, "sleep"),
            mock.patch.object(
                sony_control.time, "monotonic", side_effect=[0.0, 0.0, 0.5]
            ),
        ):
            receiver.wake_from_standby(wait=1.0)

        self.assertEqual(receiver.sent, [sony_control.POWER_TOGGLE])
        self.assertEqual(receiver.power_state.call_count, 2)


class CommandLineTest(unittest.TestCase):
    def run_cli(self, args, receiver, standby_source=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        environment = {}
        if standby_source is not None:
            environment["SONY_STANDBY_SOURCE"] = standby_source
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(sys, "argv", ["sony-control", *args]),
            mock.patch("sony_control.Receiver", return_value=receiver) as receiver_class,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as exit_status,
        ):
            runpy.run_path("sony-control", run_name="__main__")
        return (
            exit_status.exception.code,
            stdout.getvalue(),
            stderr.getvalue(),
            receiver_class,
        )

    def test_status_prints_each_classified_power_state(self):
        for state, expected in (
            (sony_control.PowerState.STANDBY, "power: standby\n"),
            (sony_control.PowerState.UNKNOWN, "power: unknown\n"),
        ):
            with self.subTest(state=state):
                receiver = mock.Mock()
                receiver.power_state.return_value = state

                code, stdout, stderr, _ = self.run_cli(
                    ["status"], receiver, standby_source="BD"
                )

                self.assertEqual(code, 0)
                self.assertEqual(stdout, expected)
                self.assertEqual(stderr, "")

    def test_passes_configured_standby_source_to_receiver(self):
        receiver = mock.Mock()
        receiver.power_state.return_value = sony_control.PowerState.STANDBY

        _, _, _, receiver_class = self.run_cli(
            ["status"], receiver, standby_source="BD"
        )

        receiver_class.assert_called_once_with(
            sony_control.DEFAULT_HOST, standby_source="BD"
        )

    def test_power_on_reports_unknown_state_without_success_output(self):
        receiver = mock.Mock()
        receiver.power_on.side_effect = sony_control.SonyError(
            "Receiver power state is unknown; configure SONY_STANDBY_SOURCE"
        )

        code, stdout, stderr, _ = self.run_cli(["power-on"], receiver)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("power state is unknown", stderr)

    def test_input_rejects_the_configured_standby_source(self):
        receiver = sony_control.Receiver("receiver", standby_source="BD")
        receiver.power_on = mock.Mock(side_effect=AssertionError("must not power on"))
        receiver.send_ircc = mock.Mock(side_effect=AssertionError("must not send IRCC"))

        code, stdout, stderr, _ = self.run_cli(
            ["input", "bd"], receiver, standby_source="BD"
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("matches the standby source", stderr)
        receiver.power_on.assert_not_called()
        receiver.send_ircc.assert_not_called()


class SelectInputTest(unittest.TestCase):
    def test_rejects_standby_source_before_power_or_input_io(self):
        for method_name in ("select_input", "select_input_when_awake"):
            with self.subTest(method=method_name):
                receiver = sony_control.Receiver("receiver", standby_source="BD")
                receiver.power_on = mock.Mock()
                receiver.source = mock.Mock()
                receiver.send_ircc = mock.Mock()

                with self.assertRaisesRegex(
                    sony_control.SonyError, "matches the standby source"
                ):
                    getattr(receiver, method_name)("bd")

                receiver.power_on.assert_not_called()
                receiver.source.assert_not_called()
                receiver.send_ircc.assert_not_called()

    def receiver_with_sources(self, sources):
        receiver = sony_control.Receiver("receiver")
        receiver.power_on = lambda: False
        sequence = iter(sources)
        receiver.source = lambda: next(sequence)
        receiver.sent = []
        receiver.send_ircc = receiver.sent.append
        return receiver

    def test_does_nothing_when_already_selected(self):
        receiver = self.receiver_with_sources(["SA-CD/CD"])

        self.assertEqual(receiver.select_input("SA-CD/CD"), 0)
        self.assertEqual(receiver.sent, [])

    def test_confirms_each_function_step(self):
        receiver = self.receiver_with_sources(["TV", "TV", "USB", "SA-CD/CD"])

        self.assertEqual(receiver.select_input("SA-CD/CD"), 2)
        self.assertEqual(
            receiver.sent,
            [sony_control.FUNCTION_PLUS, sony_control.FUNCTION_PLUS],
        )


if __name__ == "__main__":
    unittest.main()
