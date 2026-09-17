import importlib.machinery
import importlib.util
import unittest


loader = importlib.machinery.SourceFileLoader("sony_control", "sony-control")
spec = importlib.util.spec_from_loader(loader.name, loader)
sony_control = importlib.util.module_from_spec(spec)
loader.exec_module(sony_control)


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
