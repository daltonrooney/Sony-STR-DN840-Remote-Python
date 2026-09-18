import json
import ssl
from dataclasses import dataclass
from urllib import error, parse, request


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


def _normalize_scalar(value: object, name: str) -> str:
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise WiiMProtocolError(f"WiiM player status {name} must be scalar")
    return str(value).strip().lower()


def parse_player_status(payload: bytes, airplay_mode: str = "1") -> WiiMStatus:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WiiMProtocolError("Invalid WiiM player status response") from error

    if not isinstance(parsed, dict):
        raise WiiMProtocolError("WiiM player status response must be an object")
    if "status" not in parsed or "mode" not in parsed:
        raise WiiMProtocolError("WiiM player status response is missing status or mode")

    raw_status = _normalize_scalar(parsed["status"], "status")
    mode = _normalize_scalar(parsed["mode"], "mode")
    if raw_status not in KNOWN_STATES:
        raise WiiMProtocolError(f"Unknown WiiM player status: {raw_status!r}")
    return WiiMStatus(raw_status, mode, airplay_mode)


class WiiMClient:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        airplay_mode: str = "1",
        opener=None,
        *,
        tls_verify: bool = True,
    ):
        self.endpoint = parse.urljoin(
            base_url.rstrip("/") + "/", "httpapi.asp?command=getPlayerStatus"
        )
        self.timeout = timeout
        self.airplay_mode = airplay_mode
        if opener is not None:
            self.opener = opener
        elif parse.urlsplit(base_url).scheme == "https" and not tls_verify:
            context = ssl._create_unverified_context()
            self.opener = request.build_opener(
                request.HTTPSHandler(context=context)
            ).open
        else:
            self.opener = request.urlopen

    def fetch_status(self) -> WiiMStatus:
        status_request = request.Request(
            self.endpoint, headers={"Accept": "application/json"}
        )
        try:
            with self.opener(status_request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise WiiMError(
                        f"WiiM status request returned HTTP {response.status}"
                    )
                payload = response.read()
        except (error.HTTPError, error.URLError, TimeoutError, OSError) as exception:
            raise WiiMError("WiiM status request failed") from exception
        return parse_player_status(payload, self.airplay_mode)
