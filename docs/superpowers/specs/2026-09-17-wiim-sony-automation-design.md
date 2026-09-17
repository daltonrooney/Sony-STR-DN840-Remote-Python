# WiiM Mini to Sony STR-DN840 Automation Design

## Purpose

Run a small local daemon on `automation-host.example` that detects AirPlay playback beginning on a
WiiM Mini. When the Sony receiver was in standby at that moment, the daemon
wakes it, waits until it is ready, selects SA-CD/CD, and confirms the input.
Playback ending never powers the receiver off.

The daemon must coexist with the existing LG TV, HDMI-CEC, and ARC behavior. If
the Sony is already on, the initial `switch_if_off` policy leaves its current
input unchanged.

## Scope

The daemon handles one transition:

```text
WiiM not playing -> WiiM playing via AirPlay
    Sony on      -> no action
    Sony standby -> wake, await readiness, select SA-CD/CD
    Sony unknown -> no action, log the uncertainty
```

Automatic power-off, volume synchronization, TV or CEC control, Home
Assistant, MQTT, cloud APIs, playback control, and multiroom behavior are out
of scope.

## Architecture

The implementation uses synchronous Python and the standard library. A
one-to-two-second polling interval is sufficient and avoids the callback
server, subscription renewal, and reconciliation polling required by UPnP
eventing.

The existing Sony logic becomes an importable module while `sony-control`
remains the supported command-line interface.

```text
WiiMClient
    fetch_status() -> WiiMStatus

SonyController
    power_state() -> ON | STANDBY | UNKNOWN
    wake_and_wait()
    source()
    select_input(name)

AutomationController
    observe(status)
    handle_playback_started()

PollingService
    schedules polls
    contains failures
    reports transitions and recovery
```

Device protocol code is separate from policy. Tests can substitute controlled
clients without opening network connections.

## WiiM status model

The client queries:

```text
GET <WIIM_BASE_URL>/httpapi.asp?command=getPlayerStatus
```

`WIIM_BASE_URL` supports HTTP or HTTPS and an optional port. `WIIM_IP` remains
available as a convenience and initially constructs an HTTP URL. Live setup
will determine whether the installed Mini instead requires HTTPS with its
self-signed certificate.

The response must be a JSON object containing scalar `status` and `mode`
values. Values are normalized to lowercase strings. The state mapping is:

| Raw status | Playing |
| --- | --- |
| `play` | yes |
| `pause`, `stop`, `loading`, `load` | no |
| missing or unknown | invalid response |

Official WiiM documentation assigns mode `1` to AirPlay and AirPlay 2. The
initial policy requires both `status=play` and mode `1`. The mode is
configurable so actual Mini firmware behavior can be adopted without changing
code. Mode alone never indicates playback.

Invalid JSON, unexpected shapes, unknown status values, non-success HTTP
responses, TLS failures, and timeouts are poll failures. They do not change the
last valid playback state.

## Transition behavior

The controller stores the previous valid playing state in memory:

- The first valid status after process startup establishes state.
- A first valid `play` is actionable, making restarts self-healing.
- A first valid non-playing response establishes the idle baseline.
- `false -> true` invokes the playback-start policy once.
- `true -> true` does nothing.
- `true -> false` updates state and takes no Sony action.
- Failures preserve the previous valid state.
- `pause -> play` creates a new actionable transition.

No persistent transition database is needed. The desired startup behavior
intentionally treats active playback as a new event after process restart.

## Sony policy and power safety

The daemon reuses the registered CERS identity, UPnP readiness checks, IRCC
power command, and feedback-driven input selection from the existing Sony
implementation.

Power is tri-state:

- `ON`: positive responses from the receiver's active UPnP controls.
- `STANDBY`: a receiver-specific positive network signature established by
  live tests while the receiver is deliberately placed in standby.
- `UNKNOWN`: timeouts, general network failure, contradictory probes, or a
  signature that has not been validated.

A failed request alone never means standby. Before live standby
characterization, the classifier returns `UNKNOWN` rather than sending the
toggle. Characterization will compare CERS, device-description, IRCC status,
and RenderingControl behavior while on and in standby. The accepted standby
signature must include positive proof that the receiver's network stack is
responding plus repeated absence of the active-state response.

Playback-start policy:

1. Read the Sony power state.
2. If `ON`, log that the receiver is already active and stop. This applies even
   if its current input is SA-CD/CD; no command is necessary.
3. If `UNKNOWN`, log the uncertainty and stop without retrying the event.
4. If `STANDBY`, remember that this operation owns the wake and send one power
   command.
5. Poll the positive active-state probe until ready or until a bounded timeout.
6. Once ready, select the configured input with the existing CERS feedback
   loop and confirm the final source.

The operation does not reconsider the `switch_if_off` policy after its own wake.
Ownership of that wake explicitly permits the subsequent input selection.

## Configuration

Environment variables provide deployment configuration:

| Variable | Default | Meaning |
| --- | --- | --- |
| `WIIM_IP` | none | WiiM address when using the simple HTTP endpoint |
| `WIIM_BASE_URL` | derived from `WIIM_IP` | Full scheme, host, and optional port |
| `WIIM_AIRPLAY_MODE` | `1` | Firmware mode value identifying AirPlay |
| `SONY_IP` | `receiver.example` | Receiver address |
| `SONY_TARGET_INPUT` | `SA-CD/CD` | Analog input connected to the WiiM |
| `POLL_INTERVAL` | `1.5` | Seconds between completed polls |
| `REQUEST_TIMEOUT` | `1.0` | Per-request network timeout in seconds |
| `SONY_READY_TIMEOUT` | `20` | Maximum wake/readiness wait in seconds |
| `LOG_LEVEL` | `INFO` | Python logging level |

Configuration is validated at startup. Missing WiiM addressing, malformed
URLs, non-positive intervals, and invalid log levels fail fast with a clear
message.

## Failure handling and logging

The polling loop catches device and parse failures and continues running.
Successful polls do not produce per-interval logs.

The service logs:

- valid WiiM state transitions and source mode;
- first loss of WiiM connectivity and subsequent recovery;
- the Sony power classification;
- wake, readiness, input selection, and confirmation;
- bounded wake timeout or input-selection failure.

Repeated identical poll failures are suppressed until recovery or until a
long periodic reminder interval. A failed playback-start action is not retried
every poll while playback remains active. A later pause/stop followed by play,
or a process restart during playback, creates another opportunity.

Unexpected exceptions are caught at the outer polling boundary and logged.
The systemd service also restarts the process after an unexpected fatal exit.

## Testing

Unit tests use the standard library and cover:

- WiiM response parsing and normalization;
- play, pause, stop, loading, and unknown states;
- AirPlay mode matching;
- first-observation startup behavior;
- transition de-duplication;
- preservation of state across poll failures;
- Sony `ON`, `STANDBY`, and `UNKNOWN` policy branches;
- wake ownership through readiness and input selection;
- bounded timeouts and recovery after client errors;
- configuration validation.

Tests are written before production behavior and observed failing for the
expected reason.

Live validation remains a deployment gate. With the WiiM Mini installed, raw
responses are captured for idle, AirPlay connected but silent, playing,
paused, resumed, stopped, and unreachable states. HTTP versus HTTPS, field
casing, exact mode, and loading values are documented from the installed
firmware.

The live scenarios are:

1. Sony standby, WiiM idle, then AirPlay playback: wake and select SA-CD/CD.
2. Daemon starts while WiiM is already playing and Sony is in standby: act on
   the first valid status.
3. Sony on another input when playback begins: leave it unchanged.
4. Sony already on SA-CD/CD: send no command.
5. WiiM unavailable and later restored: daemon survives and resumes polling.
6. Sony unavailable: classify unknown, send no power toggle, and remain alive.
7. Playing, paused, then playing: process the second start without redundant
   Sony commands when the receiver is already on.

## Service deployment

The repository supplies a systemd unit template that:

- starts after `network-online.target`;
- runs as the unprivileged `SERVICE_USER` user;
- loads configuration from an environment file;
- restarts after unexpected failure with a short delay;
- writes logs to the journal.

The service is installed but not enabled until the live validation gate passes.
Normal operation is inspected with `systemctl status wiim-sony` and
`journalctl -u wiim-sony -f`.

## Documentation

The final README covers the RCA and LAN topology, Sony Network Standby and CERS
registration, observed WiiM Mini payloads, configuration, manual execution,
systemd installation, troubleshooting, target-input changes, and known
limitations. It distinguishes official API expectations from behavior verified
against the installed device.
