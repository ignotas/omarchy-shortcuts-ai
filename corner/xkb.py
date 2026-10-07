"""XKB keycode to keysym name, so code:20 can be matched to the key that was pressed."""

from __future__ import annotations

import ctypes
import threading
from ctypes import c_char_p, c_int, c_size_t, c_uint32, c_void_p


class _RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", c_char_p),
        ("model", c_char_p),
        ("layout", c_char_p),
        ("variant", c_char_p),
        ("options", c_char_p),
    ]


class Keysyms:
    def __init__(self, layout: str = "gb", variant: str = "") -> None:
        self._lib = ctypes.CDLL("libxkbcommon.so.0")
        lib = self._lib
        lib.xkb_context_new.restype = c_void_p
        lib.xkb_context_new.argtypes = [c_int]
        lib.xkb_keymap_new_from_names.restype = c_void_p
        lib.xkb_keymap_new_from_names.argtypes = [c_void_p, ctypes.POINTER(_RuleNames), c_int]
        lib.xkb_keymap_key_get_syms_by_level.restype = c_int
        lib.xkb_keymap_key_get_syms_by_level.argtypes = [
            c_void_p, c_uint32, c_uint32, c_uint32, ctypes.POINTER(ctypes.POINTER(c_uint32)),
        ]
        lib.xkb_keysym_get_name.restype = c_int
        lib.xkb_keysym_get_name.argtypes = [c_uint32, c_char_p, c_size_t]
        lib.xkb_keymap_unref.argtypes = [c_void_p]
        lib.xkb_context_unref.argtypes = [c_void_p]

        self._context = lib.xkb_context_new(0)
        names = _RuleNames(
            b"evdev",
            b"pc105",
            layout.encode() or b"us",
            variant.encode(),
            None,
        )
        self._keymap = lib.xkb_keymap_new_from_names(self._context, ctypes.byref(names), 0)
        self._cache: dict[int, str] = {}
        self._lock = threading.Lock()
        if not self._keymap:
            raise RuntimeError(f"no xkb keymap for layout {layout!r}")

    def name(self, keycode: int) -> str:
        with self._lock:
            return self._name_locked(keycode)

    def _name_locked(self, keycode: int) -> str:
        cached = self._cache.get(keycode)
        if cached is not None:
            return cached
        syms = ctypes.POINTER(c_uint32)()
        count = self._lib.xkb_keymap_key_get_syms_by_level(
            self._keymap, c_uint32(keycode), c_uint32(0), c_uint32(0), ctypes.byref(syms)
        )
        found = ""
        if count > 0 and syms:
            buf = ctypes.create_string_buffer(64)
            written = self._lib.xkb_keysym_get_name(syms[0], buf, len(buf))
            if written > 0:
                found = buf.value.decode()
        self._cache[keycode] = found
        return found

    def close(self) -> None:
        if self._keymap:
            self._lib.xkb_keymap_unref(self._keymap)
            self._keymap = None
        if self._context:
            self._lib.xkb_context_unref(self._context)
            self._context = None
