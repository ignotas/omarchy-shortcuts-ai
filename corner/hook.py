"""Lua installed into the running Hyprland with `hyprctl`.

It records shortcut chords only. A key with no Ctrl, Alt, or Super, below
the F1 keycode, is typing and is ignored. Repeats are ignored so holding
volume does not count as having learned it.
"""

from __future__ import annotations

from corner.model import FUNCTION_KEYCODE


def dkey_source(log_path: str, keycode: int) -> str:
    """Swallow d and Shift+D only while the corner is armed.

    hl.on watches a key and still delivers it to the focused window. hl.bind
    consumes it. Both spellings are bound because Hyprland stores the key
    string as given. A press that matches two of them is counted once.
    Ctrl, Alt, and Super chords are left alone, including Super+Shift+D.
    Each bind object is removed with :unbind(). hl.unbind("D") would also
    remove every other shortcut on D.
    """
    if any(char in log_path for char in "\n\"'\\"):
        raise ValueError("debug key path must be a plain absolute path")
    code = int(keycode)
    if code < 1 or code > 255:
        raise ValueError("debug keycode is out of range")
    # `code` is the layout's D key (XKB keycode, evdev + 8).
    return f"""
if _G.ignotas_shortcuts_ai_dkey then return "already" end
_G.ignotas_shortcuts_ai_dkey = true
local path = "{log_path}"
local keycode = {code}
local held = nil
local last_bite = 0
local function bite()
  local now = os.clock()
  if now - last_bite < 0.05 then return end
  last_bite = now
  hl.exec_cmd(string.format("printf '1\\\\n' >> '%s'", path))
end
function _G.ignotas_shortcuts_ai_set_arm(on)
  if on then
    if held then return "same" end
    local specs = {{ "d", "D", "SHIFT + d", "SHIFT + D" }}
    local binds = {{}}
    for _, spec in ipairs(specs) do
      local ok, bind = pcall(hl.bind, spec, bite, {{ description = "Corner debug" }})
      if not ok or not bind then
        for _, old in ipairs(binds) do pcall(function() old:unbind() end) end
        return "failed"
      end
      binds[#binds + 1] = bind
    end
    held = binds
    return "bound"
  end
  if not held then return "same" end
  local binds = held
  held = nil
  for _, bind in ipairs(binds) do pcall(function() bind:unbind() end) end
  return "unbound"
end
return "installed"
"""


def hook_source(log_path: str) -> str:
    if any(char in log_path for char in "\n\"'\\"):
        raise ValueError("chord log path must be a plain absolute path")
    # An f-string leaves `%%` and turns `\\\\n` into the lua source `\\n`,
    # which printf then reads as a newline. FUNCTION_KEYCODE is shared with
    # the matcher so the hook and the tests cannot drift apart.
    return f"""
if _G.ignotas_shortcuts_ai_hook then return "already" end
_G.ignotas_shortcuts_ai_hook = true
local path = "{log_path}"
local function down(name)
  local ok, value = pcall(hl.is_key_down, name)
  return ok and value and true or false
end
hl.on("input.keyboard.key", function(code, _when, how)
  if how ~= 1 then return end
  local mods = 0
  if down("Shift_L") or down("Shift_R") then mods = mods + 1 end
  if down("Control_L") or down("Control_R") then mods = mods + 4 end
  if down("Alt_L") or down("Alt_R") then mods = mods + 8 end
  if down("Super_L") or down("Super_R") then mods = mods + 64 end
  if mods < 4 and code < {FUNCTION_KEYCODE} then return end
  hl.exec_cmd(string.format("printf '%%d %%d %%d\\\\n' %d %d %d >> '%s'", code, mods, os.time(), path))
end)
return "installed"
"""
