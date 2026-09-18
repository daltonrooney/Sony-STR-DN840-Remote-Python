import argparse
import logging
import math
import os
import signal
import sys
import threading
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from sony_control import PowerState, Receiver, SonyError
from wiim_client import WiiMClient, WiiMError, WiiMStatus


class ConfigError(ValueError):
    pass


def positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{name} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = env.get(name)
    if raw_value is None:
        return default
    if not isinstance(raw_value, str):
        raise ConfigError(f"{name} must be true or false")
    value = raw_value.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class Config:
    wiim_base_url: str
    wiim_tls_verify: bool
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

        try:
            parsed = urllib.parse.urlsplit(base_url)
            parsed.port
        except (TypeError, ValueError) as error:
            raise ConfigError("WIIM_BASE_URL must be an HTTP or HTTPS URL") from error
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ConfigError("WIIM_BASE_URL must be an HTTP or HTTPS URL")

        log_level_value = env.get("LOG_LEVEL", "INFO")
        if not isinstance(log_level_value, str):
            raise ConfigError("LOG_LEVEL must be a string")
        log_level = log_level_value.upper()
        if log_level not in logging.getLevelNamesMapping():
            raise ConfigError(f"Invalid LOG_LEVEL: {log_level}")

        return cls(
            wiim_base_url=base_url.rstrip("/"),
            wiim_tls_verify=boolean(env, "WIIM_TLS_VERIFY", True),
            wiim_airplay_mode=env.get("WIIM_AIRPLAY_MODE", "1"),
            sony_ip=env.get("SONY_IP", "receiver.example"),
            target_input=env.get("SONY_TARGET_INPUT", "SA-CD/CD"),
            poll_interval=positive_float(env, "POLL_INTERVAL", 1.5),
            request_timeout=positive_float(env, "REQUEST_TIMEOUT", 1.0),
            sony_ready_timeout=positive_float(env, "SONY_READY_TIMEOUT", 20.0),
            log_level=log_level,
        )


class AutomationController:
    def __init__(
        self,
        sony: Receiver,
        target_input: str,
        sony_ready_timeout: float,
        logger: logging.Logger,
    ):
        self.sony = sony
        self.target_input = target_input
        self.sony_ready_timeout = sony_ready_timeout
        self.logger = logger
        self._previous_playing: bool | None = None

    def observe(self, status: WiiMStatus) -> None:
        playing = status.airplay_playing
        playback_started = playing and self._previous_playing is not True
        self._previous_playing = playing
        if not playback_started:
            return

        self.logger.info("playback started")
        self._handle_playback_started()

    def _handle_playback_started(self) -> None:
        state = self.sony.power_state()
        self.logger.info("Sony power state classified as %s", state.name)
        if state is PowerState.ON:
            return
        if state is PowerState.UNKNOWN:
            return
        if state is not PowerState.STANDBY:
            return

        self.logger.info("Sony receiver is in STANDBY; waking")
        self.sony.wake_from_standby(self.sony_ready_timeout)
        self.logger.info("Sony receiver is ready")
        self.logger.info("selecting input %s", self.target_input)
        self.sony.select_input_when_awake(self.target_input)
        confirmed = self.sony.source()
        self.logger.info("confirmed source %s", confirmed)
        if confirmed.casefold() != self.target_input.casefold():
            raise SonyError(f"Sony input confirmation failed: {confirmed}")


class PollingService:
    def __init__(
        self,
        wiim,
        controller: AutomationController,
        poll_interval: float,
        logger: logging.Logger,
    ):
        self.wiim = wiim
        self.controller = controller
        self.poll_interval = poll_interval
        self.logger = logger
        self._wiim_unavailable = False

    def poll_once(self) -> None:
        try:
            status = self.wiim.fetch_status()
        except WiiMError:
            if not self._wiim_unavailable:
                self.logger.warning("WiiM is unreachable")
            self._wiim_unavailable = True
            return
        except Exception:
            if not self._wiim_unavailable:
                self.logger.exception("Unexpected error polling WiiM")
            self._wiim_unavailable = True
            return

        if self._wiim_unavailable:
            self.logger.info("WiiM recovered")
            self._wiim_unavailable = False
        try:
            self.controller.observe(status)
        except SonyError:
            self.logger.exception("Sony controller action failed")
        except Exception:
            self.logger.exception("Unexpected controller error")

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.poll_once()
            stop_event.wait(self.poll_interval)


def build_service(config: Config) -> PollingService:
    wiim = WiiMClient(
        config.wiim_base_url,
        config.request_timeout,
        config.wiim_airplay_mode,
        tls_verify=config.wiim_tls_verify,
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


def main(
    argv: list[str] | None = None, env: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(description="Automate Sony input selection for WiiM AirPlay playback.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll the WiiM once and exit",
    )
    arguments = parser.parse_args(argv)

    try:
        config = Config.from_env(os.environ if env is None else env)
    except ConfigError as error:
        print(f"wiim-sony: {error}", file=sys.stderr)
        return 2

    logging.basicConfig(level=config.log_level)
    service = build_service(config)
    if arguments.once:
        service.poll_once()
        return 0

    stop_event = threading.Event()
    logger = logging.getLogger("wiim-sony")

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("received signal %s; stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    service.run_forever(stop_event)
    return 0
