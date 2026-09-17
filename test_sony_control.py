import unittest
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

    def test_owned_wake_sends_one_toggle_and_waits_for_on(self):
        receiver = sony_control.Receiver("receiver")
        receiver.sent = []
        receiver.send_ircc = receiver.sent.append
        states = iter([sony_control.PowerState.UNKNOWN, sony_control.PowerState.ON])
        receiver.power_state = lambda: next(states)

        with mock.patch.object(sony_control.time, "sleep"):
            receiver.wake_from_standby(wait=2)

        self.assertEqual(receiver.sent, [sony_control.POWER_TOGGLE])


class SelectInputTest(unittest.TestCase):
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
