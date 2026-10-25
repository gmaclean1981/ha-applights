"""AppLights wire-format research, derived from installed Android app 1.8.7.

Pure byte transformations only: this module does not operate a Bluetooth radio.
Mode 3 login and four Star SpinnerC presets match the user's capture. A local
client sent the six-step output trial and received transport acknowledgements;
the user observed visible changes without identifying each animation by name.
Other hardware/protocol variants remain unverified.
"""

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
NAME_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
TYPE_UUID = "0000fff6-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff7-0000-1000-8000-00805f9b34fb"

STAR_SPINNER_THEMES = (
    "LightShow",
    "Red",
    "Green",
    "Blue",
    "White",
    "Red & Green",
    "Red & Blue",
    "Green & Blue",
    "Red & White",
    "Green & White",
    "Blue & White",
    "Red, Green & Blue",
    "Red, Green & White",
    "Red, Blue & White",
    "Green, Blue & White",
    "Red, Green, Blue & White",
)
STAR_SPINNER_EFFECT_OFFSETS = {"Steady": 3, "Glimmer": 1, "Sparkle": 2, "Starry Night": 0}


def star_spinner_program(theme: str, effect: str) -> int:
    """Map Star SpinnerC's app menu (HomeActivity.OnSelectClicked8) to an ID."""
    if theme == "LightShow":
        if effect != "LightShow":
            raise ValueError("LightShow theme permits only the LightShow effect")
        return 0x61
    if theme not in STAR_SPINNER_THEMES or effect not in STAR_SPINNER_EFFECT_OFFSETS:
        raise ValueError("Unknown Star SpinnerC theme/effect")
    return 98 + 4 * (STAR_SPINNER_THEMES.index(theme) - 1) + STAR_SPINNER_EFFECT_OFFSETS[effect]


def address_bytes(address: str) -> bytes:
    parts = address.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError("Expected a six-byte Bluetooth address")
    return bytes(int(p, 16) for p in parts)


def advertisement_marker(address: str) -> int:
    """Match the app's signed Java-short arithmetic for scanRecord byte 8."""
    m = address_bytes(address)

    def short(value):
        return ((value + 32768) % 65536) - 32768

    value = short(short(short(m[0] * m[2]) + short(m[3] * m[5])) - short(m[1] * m[4]))
    return (1 - int(value / 2)) & 255


def transform(payload: bytes, address: str, mode: int, challenge: int | None = None) -> bytes:
    """Symmetric encoding from z.a.P; mode 3 adds the advertisement challenge."""
    if mode not in (2, 3):
        raise ValueError("Only protected protocol modes 2 and 3 are implemented")
    key = address_bytes(address)
    if key[-1] & 1:
        key = key[::-1]
    if mode == 3 and (challenge is None or not 0 <= challenge <= 255):
        raise ValueError("Mode 3 requires a fresh advertisement challenge byte")
    extra = challenge if mode == 3 else 0
    return bytes(value ^ key[index % 6] ^ extra for index, value in enumerate(payload))


def login_packet(password: str, address: str, mode: int, challenge: int | None = None) -> bytes:
    """Encode the owner's existing password, without changing or resetting it."""
    encoded = password.encode("utf-8")
    if not 1 <= len(encoded) <= 8:
        raise ValueError("AppLights login permits 1–8 UTF-8 bytes")
    body = bytes([0x73]) + bytes(value ^ 0xA5 for value in encoded + bytes([len(encoded)]))
    return transform(body, address, mode, challenge)


def program_packet(program: int, address: str, mode: int, challenge: int | None = None) -> bytes:
    """Format only; caller must establish model-specific program IDs separately."""
    if not 0 <= program <= 255:
        raise ValueError("Program must fit in one byte")
    # Java promotes byte to signed int before >>; IDs >= 128 use F8..FF.
    signed_program = program if program < 128 else program - 256
    payload = bytes([0x67, (0xA0 | (signed_program >> 4)) & 255, 0xA0 | (program & 15), 0])
    return transform(payload, address, mode, challenge)


def describe_packet(payload: bytes) -> dict:
    """Summarize decoded packets, suppressing password-bearing bodies."""
    if not payload:
        return {"kind": "empty"}
    op = payload[0]
    if op == 0x73:
        return {"kind": "login", "password_redacted": True, "length": len(payload)}
    if op in (0x79, 0x7B, 0x7C, 0x85):
        return {"kind": "password_or_configuration", "body_redacted": True, "opcode": f"{op:02x}"}
    if op == 0x37 and len(payload) >= 2:
        result = {0xA5: "accepted", 0x5A: "rejected"}.get(payload[1], "unknown")
        return {"kind": "login_result", "result": result}
    if (
        op == 0x67
        and len(payload) == 4
        and payload[1] in (*range(0xA0, 0xA8), *range(0xF8, 0x100))
        and payload[2] & 0xF0 == 0xA0
    ):
        program = ((payload[1] & 15) << 4) | (payload[2] & 15)
        return {
            "kind": "program",
            "program": program,
            "program_hex": f"{program:02x}",
            "app_mapping": {0x60: "on", 0xFF: "off"}.get(program, "effect"),
        }
    if op == 0x98 and len(payload) == 2:
        return {"kind": "brightness", "raw_value": payload[1]}
    if payload == bytes([0x6C, 0x55]):
        return {"kind": "query_password_enabled"}
    return {"kind": "unmapped", "opcode": f"{op:02x}", "length": len(payload)}
