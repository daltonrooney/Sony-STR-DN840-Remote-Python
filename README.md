# Sony STR-DN840 LAN control

`sony-control` provides verified LAN control for the receiver at
`receiver.example` using only the Python standard library.

```console
./sony-control status
./sony-control power-on
./sony-control input SA-CD/CD
```

Set `SONY_HOST` or pass `--host` to use another address. Registration stores the
CERS client ID in `.sony-control-device-id`. Set `SONY_DEVICE_ID` to import or
override an existing registration.

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

The automation watches the WiiM Mini's LAN status endpoint. When AirPlay
playback starts while the Sony is in standby, it wakes the receiver and selects
the configured input. Wire the playback path as:

```text
AirPlay source -> WiiM Mini -> RCA -> Sony SA-CD/CD
```

Both devices must be reachable on the LAN through `automation-host.example`: the WiiM HTTP status
endpoint and the Sony CERS endpoint on port 50001 plus IRCC on port 8080.
Enable Network Standby on the Sony so it can receive the wake command. Complete
CERS registration before running the automation, using the registration steps
above.

The expected WiiM status for AirPlay playback is `status=play` and mode `1`.
Those values, and the Sony standby classification used before waking it, await
live confirmation against the installed WiiM firmware and receiver. Do not
enable the service yet.

The automation never powers the receiver off. It also makes no input change
when the Sony is already on; it acts only on a transition into matching AirPlay
playback while the receiver is classified as standby.

### Configuration

Copy the example environment file and edit the network values:

```console
sudo install -m 600 systemd/wiim-sony.env.example /etc/wiim-sony.env
sudoedit /etc/wiim-sony.env
```

`WIIM_IP` is the WiiM hostname or IP address and is used to build its HTTP URL.
Set `WIIM_BASE_URL` instead to provide the complete `http://` or `https://`
base URL; it overrides `WIIM_IP`. `SONY_IP` is the receiver hostname or IP.
`SONY_TARGET_INPUT` is the source name to confirm after waking; use the CERS
source spelling, such as `SA-CD/CD`.

`POLL_INTERVAL` is the delay in seconds between WiiM polls. `REQUEST_TIMEOUT`
is the per-request timeout in seconds. `SONY_READY_TIMEOUT` is the maximum
seconds to wait after issuing the Sony wake command. `WIIM_AIRPLAY_MODE` is the
WiiM player mode that identifies AirPlay and defaults to `1`; live firmware
confirmation is still required. `LOG_LEVEL` is a standard Python logging level
such as `INFO` or `DEBUG`.

### Manual operation

Load the variables in your shell, then perform one bounded poll:

```console
set -a
. /etc/wiim-sony.env
set +a
./wiim-sony --once
```

Run `./wiim-sony` without `--once` for normal foreground polling. Stop it with
Ctrl-C. A WiiM outage is logged once and polling resumes when the endpoint
recovers.

### Service files

Install the unit and environment file only for review; do not enable or start
the service until Task 8 live validation confirms the WiiM firmware behavior
and Sony standby classification.

```console
sudo install -D -m 644 systemd/wiim-sony.service /etc/systemd/system/wiim-sony.service
sudo install -m 600 systemd/wiim-sony.env.example /etc/wiim-sony.env
sudo systemctl daemon-reload
sudo systemctl status wiim-sony
journalctl -u wiim-sony -f
```

After the live-validation gate is complete, enable it with
`sudo systemctl enable --now wiim-sony`.

### Troubleshooting

If CERS reports HTTP 403 or a registration-required error, reopen `HOME NETWORK
> OPTIONS > TV SideView Device Registration > Start Registration` on the Sony
and run `./sony-control register` within 30 seconds. Ensure the saved device ID
uses the `MediaRemote:XX-XX-XX-XX-XX-XX` form.

For `WiiM is unreachable`, check its IP or hostname, LAN route through `automation-host.example`,
and `WIIM_BASE_URL` if set. A malformed or unknown WiiM status is treated as a
polling failure; inspect the status endpoint and confirm the installed firmware
reports scalar `status` and `mode` fields.

`Sony power state classified as UNKNOWN` means the controller cannot safely
tell whether the receiver is awake, so it sends no wake or input command. Check
Sony LAN access and Network Standby. A wake timeout means the receiver did not
become reachable before `SONY_READY_TIMEOUT`; verify Network Standby and
increase that timeout only if the receiver is otherwise reachable.

An input confirmation failure means CERS did not report `SONY_TARGET_INPUT`
after the Function+ cycle. Check the source spelling with `./sony-control
status`, then set `SONY_TARGET_INPUT` to that exact source name and retry after
the live-validation gate.
