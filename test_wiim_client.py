import socket
import ssl
import unittest
from unittest import mock
from urllib import request
from urllib.error import HTTPError, URLError

from wiim_client import WiiMClient, WiiMError, WiiMProtocolError, parse_player_status


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

    def test_configured_airplay_mode_controls_actionability(self):
        status = parse_player_status(
            b'{"status":"play","mode":"2"}', airplay_mode="2"
        )
        self.assertTrue(status.airplay_playing)
        self.assertFalse(parse_player_status(b'{"status":"play","mode":"2"}').airplay_playing)

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(WiiMProtocolError):
            parse_player_status(b'{"status":"buffering","mode":"1"}')

    def test_missing_or_non_object_payload_is_rejected(self):
        for payload in (b'{}', b'[]', b'not-json'):
            with self.subTest(payload=payload):
                with self.assertRaises(WiiMProtocolError):
                    parse_player_status(payload)

    def test_boolean_and_container_status_fields_are_rejected(self):
        for payload in (
            b'{"status":true,"mode":"1"}',
            b'{"status":[],"mode":"1"}',
            b'{"status":"play","mode":false}',
            b'{"status":"play","mode":{}}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(WiiMProtocolError):
                    parse_player_status(payload)


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
            calls.append((request.full_url, timeout, request.get_header("Accept")))
            return FakeResponse(b'{"status":"play","mode":"1"}')

        client = WiiMClient("http://wiim.local", 0.8, opener=opener)
        self.assertTrue(client.fetch_status().airplay_playing)
        self.assertEqual(
            calls,
            [
                (
                    "http://wiim.local/httpapi.asp?command=getPlayerStatus",
                    0.8,
                    "application/json",
                )
            ],
        )

    def test_verified_https_uses_the_default_opener(self):
        client = WiiMClient("https://wiim.local", 0.8)

        self.assertIs(client.opener, request.urlopen)

    def test_http_ignores_disabled_tls_verification(self):
        with mock.patch.object(ssl, "_create_unverified_context") as context_factory:
            client = WiiMClient("http://wiim.local", 0.8, tls_verify=False)

        self.assertIs(client.opener, request.urlopen)
        context_factory.assert_not_called()

    def test_unverified_https_uses_an_isolated_ssl_opener(self):
        fake_opener = mock.Mock()
        fake_opener.open.return_value = FakeResponse(b'{"status":"play","mode":"1"}')
        context = mock.sentinel.context
        handler = mock.sentinel.handler

        with (
            mock.patch.object(
                ssl, "_create_unverified_context", return_value=context
            ) as context_factory,
            mock.patch.object(
                request, "HTTPSHandler", return_value=handler
            ) as handler_factory,
            mock.patch.object(
                request, "build_opener", return_value=fake_opener
            ) as opener_factory,
        ):
            client = WiiMClient("https://wiim.local", 0.8, tls_verify=False)
            status = client.fetch_status()

        self.assertTrue(status.airplay_playing)
        context_factory.assert_called_once_with()
        handler_factory.assert_called_once_with(context=context)
        opener_factory.assert_called_once_with(handler)
        fake_opener.open.assert_called_once_with(mock.ANY, timeout=0.8)

    def test_positional_injected_opener_remains_supported(self):
        injected_opener = mock.Mock(
            return_value=FakeResponse(b'{"status":"stop","mode":"1"}')
        )

        client = WiiMClient("http://wiim.local", 0.8, "1", injected_opener)
        client.fetch_status()

        injected_opener.assert_called_once_with(mock.ANY, timeout=0.8)

    def test_injected_opener_is_preserved_for_unverified_https(self):
        injected_opener = mock.Mock(
            return_value=FakeResponse(b'{"status":"stop","mode":"1"}')
        )

        with mock.patch.object(ssl, "_create_unverified_context") as context_factory:
            client = WiiMClient(
                "https://wiim.local",
                0.8,
                tls_verify=False,
                opener=injected_opener,
            )
            client.fetch_status()

        context_factory.assert_not_called()
        injected_opener.assert_called_once_with(mock.ANY, timeout=0.8)

    def test_network_failures_raise_error(self):
        for failure in (URLError("offline"), TimeoutError(), socket.timeout(), OSError()):
            with self.subTest(failure=failure):
                def opener(request, timeout, failure=failure):
                    raise failure

                with self.assertRaises(WiiMError):
                    WiiMClient("http://wiim.local", 0.8, opener=opener).fetch_status()

    def test_non_success_response_raises_error(self):
        client = WiiMClient(
            "http://wiim.local", 0.8, opener=lambda request, timeout: FakeResponse(b"{}", 503)
        )
        with self.assertRaises(WiiMError):
            client.fetch_status()

    def test_http_error_raises_error(self):
        def opener(request, timeout):
            raise HTTPError(request.full_url, 500, "failed", {}, None)

        with self.assertRaises(WiiMError):
            WiiMClient("http://wiim.local", 0.8, opener=opener).fetch_status()

    def test_invalid_response_payload_raises_error(self):
        client = WiiMClient(
            "http://wiim.local", 0.8, opener=lambda request, timeout: FakeResponse(b"not-json")
        )
        with self.assertRaises(WiiMError):
            client.fetch_status()
