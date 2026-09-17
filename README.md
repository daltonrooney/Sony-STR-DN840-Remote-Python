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
