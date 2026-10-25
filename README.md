# AppLights for Home Assistant

<img src="https://raw.githubusercontent.com/gmaclean1981/ha-applights/main/custom_components/applights/brand/icon.png" width="96" alt="AppLights community integration icon">

Local Bluetooth control for the **AppLights Star SpinnerC**, reporting protocol
**BTM2-5**. Control the light directly from Home Assistant using a local Bluetooth
adapter or an active ESPHome Bluetooth proxy. No cloud account, MQTT bridge or
running phone app is required for control.

**AI-agent disclosure:** This project was coded using an **AI coding agent
(OpenAI Codex)**, with protocol research and hands-on testing by the device owner.

This is an independent community integration, not an official AppLights product or
an integration shipped with Home Assistant. Only the model below has been tested.

## Features and compatibility

| Feature | Support |
| --- | --- |
| Tested light | Advertises as **Star SpinnerC**, identifies as **BTM2-5** |
| Power | On/off |
| Effects | 61 named theme/effect combinations in the light's Effect selector |
| Authentication | Existing password or explicit factory-default login; optional verified password changes |
| Bluetooth routing | Home Assistant adapters and active ESPHome proxies |
| Responsiveness | Authenticated session held for **30 seconds after the last successful command** |
| State | Assumed: the last successful command, without independent readback |
| Diagnostics | Connection/reuse counts, idle release counts and command timing |

BTM2-5 is a protocol identifier, **not a firmware version**. Other AppLights models,
including similarly named Star Spinner variants, are not established as compatible.
There is no brightness slider, free RGB picker or speed control for this model.

## Install with HACS

Requires Home Assistant **2026.10.2 or newer**, HACS, and a working Bluetooth adapter
or active ESPHome proxy within range of the light.

1. In **HACS**, open the top-right menu and choose **Custom repositories**.
2. Add `https://github.com/gmaclean1981/ha-applights` with type **Integration**.
3. Find **AppLights**, download the latest release, and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → AppLights**, or use
   the discovered device card.
5. Close the AppLights phone app, choose the light and enter its current password,
   or select **Use factory-default password (000000)** and leave the password field
   blank. That selection only attempts a login; it does not reset the light.
6. Optionally select **Set a new password after connecting**. The next screen asks
   for the new password twice. Submitting that screen changes the device password,
   then verifies a fresh login before saving it. Setup never changes the output.

This repository is installed as a **HACS custom repository**; it is not listed in
the default HACS catalogue.

### Password settings (v0.2.0)

Open the existing AppLights integration's configuration/options menu:

- **Change the light's password** authenticates using HA's saved password, sends
  one change to this device, disconnects, and checks a new login before saving it.
- **Update saved password** verifies a password without changing the device. Use
  this after a change made in the phone app, a physical reset, or an interrupted
  change. The factory-default checkbox is available here too.

Passwords must contain 1–8 UTF-8 bytes (up to eight plain ASCII characters). The
two new entries must match and differ from the current password. Password fields
are masked and are not populated with stored values. Other controllers will need
the new password after a successful change.

If delivery is ambiguous, the integration attempts a fresh login with the proposed
password; only if explicitly rejected does it try the previous password. It never
automatically repeats a password-change write. If neither can be verified, normal
commands pause and **Verify the password change** offers verification-only retries.
If the previous password still works, the form reports that the change was not
applied; another write requires an explicit new submission.

Keep both passwords until completion. Pending recovery credentials are retained
only in memory, not in diagnostics or persistent settings. After cancelling an
initial setup or restarting HA during an unresolved change, use setup or **Update
saved password** with the proposed password first, then the previous one if the
light rejects it. A forgotten password still requires the manufacturer's physical
reset procedure; there is no remote reset or authentication bypass.

These functions were tested locally in 0.2.0b1 before release. The owner confirmed a successful password change and
correct rejection of matching-current and mismatched new passwords on the real
Star SpinnerC. Post-change diagnostics confirmed fresh authentication, on/off
command delivery and idle connection release. Factory-default onboarding and
interrupted-change recovery are covered by automated tests, but have not yet
been exercised on a factory-default device or under an induced hardware failure.

### Existing manual installation

Back up `custom_components/applights`, then download this repository through HACS
and restart Home Assistant. Keep the existing AppLights integration entry; do not
delete and re-pair it. The domain and entity unique IDs are unchanged from the
local 0.1.0 beta builds, so the existing device, password and entity are retained.

### Manual installation

Copy `custom_components/applights` from a release into your Home Assistant
`config/custom_components/` directory, restart, then follow setup steps 4–6 above.
Do not copy tests or the repository root into `custom_components`.

## Use and connection behaviour

The integration creates one light entity. Open its controls to select an Effect.
Choosing an effect turns the light on. Ordinary On sends the device's On command
and leaves the effect name unknown. Automations use the standard `light.turn_on`
and `light.turn_off` actions; use the entity ID assigned in your own installation.

The first command obtains a fresh advertisement challenge and authenticates. Later
commands within 30 seconds reuse that session and restart the idle timer. After
inactivity, the connection closes and releases the proxy slot. **The effect keeps
playing when the connection closes.** The next command reconnects if necessary.
Setup verification, integration unload and Home Assistant shutdown close the
connection immediately. Failed program writes are not automatically replayed.

## State, privacy and troubleshooting

The observed protocol acknowledges command delivery but does not report the current
power or preset. Home Assistant therefore marks the state as **assumed**. It starts
unknown after restart and becomes unknown after a failed command or lost
reachability. Changes made with the phone app or physical controls cannot be read
back. A successful Bluetooth write is not independent proof of the LEDs' state.

- Keep the AppLights phone app closed while controlling the light from HA.
- If setup fails, check the existing password, power and Bluetooth range. An
  ESPHome proxy must support active connections. Moving a proxy closer resolved
  connection failures in the hardware trial.
- After using HA, allow the 30-second hold to expire before reconnecting the phone.
- A first press after inactivity can take longer because discovery, connection and
  authentication are required. The hold improves consecutive presses, not radio range.
- Download diagnostics from the device page when reporting a problem. Counters
  reset on integration reload, and timings exclude browser and physical LED delays.

The existing password is stored in HA's configuration entry. This integration's
diagnostic data excludes passwords, Bluetooth addresses and packet contents.
Do not post phone bugreports, raw Bluetooth captures or HA configuration entries:
they can contain credentials and unrelated personal data.

For support, [open an issue](https://github.com/gmaclean1981/ha-applights/issues)
with the advertised model, protocol identifier if known, HA version, integration
version, adapter/proxy type and a description of the problem. Review attachments
before sharing. The integration has no firmware update or raw-command interface.

To remove it, delete its entry in **Settings → Devices & services**, remove the
download in HACS, then restart Home Assistant. Removing the entry also removes its
saved configuration; it does not reset the light's password.

## Development and validation

This project was coded using an **AI coding agent (OpenAI Codex)**. Protocol work
used the owner's installed Android AppLights 1.8.7 app and authorized Bluetooth
captures. The owner performed physical checks; AI-generated code is not a claim
of universal compatibility. Vendor APKs/source, captures, bugreports, actual device
identifiers and credentials are not included. The community icon is original artwork.

The first release packages the locally tested 0.1.0b4 control behaviour:

- **20 offline tests** cover wire vectors, model/password checks, HA setup and light
  actions, session reuse, idle expiry, dropped links, cancellation, write failures
  and unload. Test harness: Home Assistant 2026.10.2.
- Hardware trial on **Home Assistant 2026.10.3** through an active ESPHome proxy:
  on/off, Green/Steady and LightShow were visually confirmed during local testing.
  Four preset frames were matched against app captures. All 61 menu combinations
  are available, but they have not all been individually verified on the light.
- With the final **30-second hold**, On took 1,610.5 ms. After a measured 25.3-second
  pause, Off reused the same connection and took 193 ms. The owner confirmed the
  light stayed on during the pause and then switched off. The timer restarted and
  diagnostics confirmed idle disconnection with no disconnect errors. These are
  sample transport timings, not guaranteed physical response times.

Version 0.2.0 adds 20 password-management tests, for **40 offline tests** in total.
They cover factory-default onboarding, optional setup and later password changes,
validation, lost acknowledgements, cancellation, recovery without replaying a
change, and updating saved credentials. The owner confirmed a real password change
and validation errors; post-change diagnostics confirmed authentication, on/off
delivery and idle release. Failure recovery and factory-default onboarding have
automated coverage but have not been exercised under those conditions on hardware.

For development on Linux with Python 3.14:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pytest --disable-socket --allow-unix-socket
ruff check .
ruff format --check .
```

Tests use synthetic credentials and prohibit real Bluetooth connections. GitHub
Actions also checks the integration with hassfest and HACS validation.

## License

[MIT](LICENSE). AppLights and other product names belong to their respective owners.
