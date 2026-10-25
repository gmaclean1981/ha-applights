# Changelog

## 0.2.0 — 2026-10-25

Adds factory-default onboarding and password management to the existing local BLE integration.
Upgrade through HACS and restart Home Assistant; keep the existing AppLights entry and entity.

- Explicit factory-default login during discovery/manual setup; does not reset the light.
- Optional new password during onboarding and later password changes through integration options.
- Fresh-login verification before saving a changed password, with verification-only recovery
  after ambiguous writes; no automatic change replay.
- Update-saved-password flow for external changes or physical resets, preserving the device/entity.
- Password matching and UTF-8 byte-limit validation; no credentials in diagnostics or error text.
- Fixed the README icon URL for HACS rendering.
- Validation: 40 offline tests and lint passed. The owner confirmed a real password change,
  same-current-password rejection and mismatch handling. Post-change diagnostics confirmed
  fresh authentication, two successful on/off commands and idle release without disconnect errors.
  Factory-default onboarding and interrupted-change recovery remain automated-test coverage only.
- Developed using an AI coding agent (OpenAI Codex).

## 0.1.0 — 2026-10-25

First public release, packaging the locally tested 0.1.0b4 behaviour.

- Local BLE on/off and 61 named theme/effect combinations for **Star SpinnerC / BTM2-5**.
- Setup through Bluetooth discovery or manual selection using the existing password.
- Home Assistant Bluetooth adapter selection and active ESPHome proxy support.
- A **30-second idle connection hold** for responsive consecutive commands, with
  automatic release, fresh authentication after reconnect and no automatic replay
  of failed program writes.
- Explicit assumed state, reauthentication support and sanitized diagnostics.
- HACS custom-repository packaging, installation instructions and validation workflow.

**AI-agent disclosure:** This project was coded using an **AI coding agent (OpenAI
Codex)**, with protocol research and hands-on testing by the device owner.

Validation: 20 offline tests and lint passed. Live local testing confirmed on/off,
selected effects, a successful command after a 25-second pause, timer restart and
automatic connection release. Only the stated model is supported; not every preset
has been individually verified. The device's current state is not independently
read back.
