# WiiM Mini local status observations

The primary WiiM Mini is available at `wiim.example`. A secondary WiiM at
`secondary-wiim.example` is not monitored by this automation.

The primary unit exposes its local HTTP API over HTTPS with a self-signed
certificate. Plain HTTP requests fail, so the local configuration uses:

```dotenv
WIIM_BASE_URL=https://wiim.example
WIIM_TLS_VERIFY=false
```

`GET /httpapi.asp?command=getPlayerStatus` has produced these values on the
installed device:

| Condition | `status` | `mode` |
| --- | --- | --- |
| Idle/stopped | `stop` | `1` |
| AirPlay playing | `play` | `1` |

The daemon treats only `status=play` with mode `1` as active AirPlay. It acts on
the transition into that state, including the first valid status after process
startup. It preserves the last valid state across connection, TLS, JSON, and
schema failures.

Player responses include track and artist metadata encoded as hexadecimal text.
The automation does not decode or depend on metadata, volume, duration, or
playback position.
