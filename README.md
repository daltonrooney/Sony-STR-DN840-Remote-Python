# Sony STR-DN840 LAN control

`sony-control` provides verified LAN control for the receiver at
`receiver.example` using only the Python standard library.

```console
./sony-control status
./sony-control power-on
./sony-control input SA-CD/CD
```

Set `SONY_HOST` or pass `--host` to use another address. Set
`SONY_STANDBY_SOURCE=BD` to enable the verified power classifier for this
installation. Without that variable, power is reported as unknown and commands
that could toggle power refuse to run. Registration stores the CERS client ID in
`.sony-control-device-id`. Set `SONY_DEVICE_ID` to import or override an existing
registration.

## CERS registration

Input status requires a one-time registration. On the receiver, select:

```text
HOME NETWORK > OPTIONS > TV SideView Device Registration > Start Registration
```

Then, within 30 seconds, run:

```console
./sony-control register
```

This registration menu is separate from DLNA Access Settings and Auto Access.
The DN840 requires the ID form `MediaRemote:XX-XX-XX-XX-XX-XX`; colon-separated
MAC bytes can appear in the registered-device list but receive HTTP 403 from
authorized CERS actions.

## Protocol findings

The receiver advertises these services:

- CERS on port 50001: registration, system information, remote-command list,
  status, and text actions. Only system information is available before
  registration. CERS status supplies the current input.
- IRCC on port 8080: remote-key commands, including power toggle and
  Function+/Function-. The receiver rejects the Sony discrete SA-CD/CD code
  `AAAAAgAAADAAAAAlAQ==` with UPnP error 802 even though it accepts the known
  volume and Function codes using the same request.
- UPnP RenderingControl and AVTransport on port 8080: volume, mute, and media
  transport. The advertised action lists contain no receiver-input setter.

No direct or idempotent SA-CD/CD setter is exposed by the receiver's CERS,
IRCC, UPnP, Party, or media-server interfaces. `sony-control input` therefore
reads CERS status, sends one Function+ command, and reads status again after
every step. It stops only when the requested input is confirmed, so it does not
depend on a hard-coded input order and is safe to call repeatedly.

## WiiM playback automation

The automation watches the WiiM Mini's LAN status endpoint. An AirPlay playback
transition wakes the Sony and selects SA-CD/CD only when the configured CERS
source sentinel classifies the receiver as being in standby. Wire the playback
path as:

```text
AirPlay source -> WiiM Mini -> RCA -> Sony SA-CD/CD
```

Both devices must be reachable on the LAN through `automation-host.example`: the WiiM HTTPS status
endpoint and the Sony CERS endpoint on port 50001 plus IRCC on port 8080.
Enable Network Standby on the Sony so it can receive the wake command. Complete
CERS registration before running the automation, using the registration steps
above.

The primary WiiM Mini is at `wiim.example` and runs firmware
`<FIRMWARE_VERSION>`. It reports `securemode=1` and `security=https/2.0`, refuses
HTTP status requests, and presents a self-signed certificate over HTTPS.
Configure its address and local certificate handling as:

```dotenv
WIIM_BASE_URL=https://wiim.example
WIIM_TLS_VERIFY=false
```

The WiiM at `secondary-wiim.example` is named secondary WiiM and is outside this automation.

Disabling certificate verification keeps the connection encrypted but does not
authenticate the WiiM. Use this setting only for the WiiM on a trusted LAN.

The primary device reports `status=stop` while idle and `status=play` with
mode `1` during AirPlay. Track and artist metadata are hex encoded. The daemon
uses only `status` and `mode`; malformed responses are ignored until a later
valid poll.

The STR-DN840 keeps its LAN services active in Network Standby, so reachability
and volume do not distinguish standby from on. In this installation, CERS
reports the stale source `BD` in standby and the current source while on. Set
`SONY_STANDBY_SOURCE=BD` only while the BD input remains unused. Unset it to
disable automatic wake safely; the receiver will be classified as unknown and
no power command will be sent.

The automation never powers the receiver off. It also makes no input change
when the Sony is already on; it acts only on a transition into matching AirPlay
playback while the receiver is classified as standby.

### Configuration

For foreground and one-shot validation, create a user-owned local environment
file and edit the network values:

```console
cp systemd/wiim-sony.env.example .wiim-sony.env
chmod 600 .wiim-sony.env
$EDITOR .wiim-sony.env
```

`WIIM_BASE_URL` is the complete WiiM status API base URL. For the primary
unit, set it to `https://wiim.example`. `WIIM_TLS_VERIFY` defaults to `true`;
set it to `false` for the WiiM's self-signed certificate. `WIIM_IP` remains
available as a fallback that builds an `http://` URL, and `WIIM_BASE_URL`
overrides it. `SONY_IP` is the receiver hostname or IP. `SONY_TARGET_INPUT` is
the source name to confirm after waking; use the CERS source spelling, such as
`SA-CD/CD`.

`SONY_STANDBY_SOURCE` is the CERS source value that indicates Network Standby.
It must differ from `SONY_TARGET_INPUT`. `BD` is verified for this topology and
requires keeping the BD input unused. An empty or unset value disables power
classification and wake commands. Both `sony-control input` and the daemon
reject a target matching the configured standby source.

`POLL_INTERVAL` is the delay in seconds between WiiM polls. `REQUEST_TIMEOUT`
is the per-request timeout in seconds. `SONY_READY_TIMEOUT` is the maximum
seconds to wait after issuing the Sony wake command. `WIIM_AIRPLAY_MODE` is the
WiiM player mode that identifies AirPlay and defaults to `1`. `LOG_LEVEL` is a
standard Python logging level
such as `INFO` or `DEBUG`.

### Manual operation

Load the user-owned local variables in your shell, then perform one bounded
poll:

```console
set -a
. ./.wiim-sony.env
set +a
./wiim-sony --once
```

Run `./wiim-sony` without `--once` for normal foreground polling. Stop it with
Ctrl-C. A WiiM outage is logged once and polling resumes when the endpoint
recovers.

### Service files

After confirming the foreground wake and input behavior, install the root-owned
environment file with mode 600 and the unit:

```console
sudo install -D -m 644 systemd/wiim-sony.service /etc/systemd/system/wiim-sony.service
sudo install -m 600 systemd/wiim-sony.env.example /etc/wiim-sony.env
sudoedit /etc/wiim-sony.env
sudo systemctl daemon-reload
sudo systemctl enable --now wiim-sony
sudo systemctl status wiim-sony
journalctl -u wiim-sony -f
```

The service remains unprivileged by running as `SERVICE_USER` while reading the
root-owned environment file.

### Troubleshooting

If CERS reports HTTP 403 or a registration-required error, reopen `HOME NETWORK
> OPTIONS > TV SideView Device Registration > Start Registration` on the Sony
and run `./sony-control register` within 30 seconds. Ensure the saved device ID
uses the `MediaRemote:XX-XX-XX-XX-XX-XX` form.

For `WiiM is unreachable`, check its IP or hostname, the LAN route through
`automation-host.example`, and that `WIIM_BASE_URL` uses HTTPS. A certificate verification failure
requires either a trusted certificate or `WIIM_TLS_VERIFY=false` for this local
self-signed device. A malformed or unknown WiiM status is treated as a polling
failure; inspect the status endpoint and confirm the installed firmware reports
scalar `status` and `mode` fields.

`Sony power state classified as UNKNOWN` means the controller cannot safely
tell whether the receiver is awake, so it sends no wake or input command. Check
CERS registration and LAN access, then confirm `SONY_STANDBY_SOURCE=BD` is set
if BD remains unused. A wake timeout means CERS continued reporting the standby
sentinel through `SONY_READY_TIMEOUT`; verify Network Standby and the sentinel
before changing the timeout.

An input confirmation failure means CERS did not report `SONY_TARGET_INPUT`
after the Function+ cycle. Check the source spelling with `./sony-control
status`, then set `SONY_TARGET_INPUT` to that exact source name and retry.
