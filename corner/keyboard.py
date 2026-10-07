"""Keys the connected keyboard can actually press.

Hyprland's bind list includes keycodes that a generic keymap names, such as
F23 for keycode 201. Those names are not keys. The kernel publishes the
keyboard's key bitmap in /proc/bus/input/devices, which is readable without
joining the input group.
"""

from __future__ import annotations

from pathlib import Path

DEVICES = Path("/proc/bus/input/devices")

# XKB keycodes are evdev codes shifted by 8. Keycode 10 is the "1" key.
EVDEV_OFFSET = 8


def key_bits(mask: str, width: int = 64) -> set[int]:
    """Decode one `B: KEY=` line. Words are unsigned longs, highest first."""
    found: set[int] = set()
    words = [int(part, 16) for part in mask.split()]
    for index, word in enumerate(words):
        base = (len(words) - 1 - index) * width
        for bit in range(width):
            if word & (1 << bit):
                found.add(base + bit)
    return found


def _device_blocks(text: str):
    for block in text.split("\n\n"):
        name = ""
        handlers = ""
        mask = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
            elif line.startswith("B: KEY="):
                mask = line.split("=", 1)[1].strip()
        if mask and "kbd" in handlers.split():
            yield name, mask


def evdev_codes(text: str) -> set[int]:
    """Evdev codes for keys that physically exist.

    The internal keyboard is the source. Power and sleep buttons are real
    keys too. A virtual keyboard advertises every keycode, so it is ignored,
    and so is the firmware video bus.
    """
    found: set[int] = set()
    saw_keyboard = False
    fallback: set[int] = set()
    for name, mask in _device_blocks(text):
        low = name.casefold()
        if "virtual" in low or "video" in low:
            continue
        bits = key_bits(mask)
        if "apple spi keyboard" in low:
            found |= bits
            saw_keyboard = True
        elif "power" in low or "sleep" in low:
            found |= bits
        elif not fallback:
            fallback = bits
    if not saw_keyboard:
        found |= fallback
    return found


def apple_hardware(vendor: str = "", product: str = "", devices: str = "") -> bool:
    """True on an Apple machine or an Apple keyboard.

    Super is the Command key there, and Alt is Option.
    """
    if "apple" in vendor.casefold():
        return True
    product_l = product.casefold()
    if "macbook" in product_l or "imac" in product_l:
        return True
    low = devices.casefold()
    return "apple" in low and "keyboard" in low


def mac_keyboard() -> bool:
    """This computer uses Mac key names. Missing DMI information stays false."""
    vendor = _dmi("sys_vendor")
    product = _dmi("product_name")
    try:
        devices = DEVICES.read_text(encoding="utf-8", errors="replace")
    except OSError:
        devices = ""
    return apple_hardware(vendor, product, devices)


def _dmi(name: str) -> str:
    try:
        return Path("/sys/class/dmi/id", name).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def pressable_names(keysyms, text: str | None = None) -> set[str] | None:
    """Keysym names this keyboard can produce, lowercased. None if unknown."""
    if text is None:
        try:
            text = DEVICES.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    codes = evdev_codes(text)
    if not codes:
        return None
    names: set[str] = set()
    for code in codes:
        name = keysyms.name(code + EVDEV_OFFSET)
        if name:
            names.add(name.casefold())
    return names or None
