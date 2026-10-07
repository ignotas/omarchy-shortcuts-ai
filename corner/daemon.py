"""Hot-corner process. Quickshell talks to it on stdin and stdout, one JSON line each.

The pointer is read from Hyprland's socket, so the corner does not have to
cover the screen and steal clicks. A suggestion is started while the pointer
is still approaching, and the card is shown only in the last few pixels.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from corner.hook import dkey_source, hook_source
from corner.keyboard import mac_keyboard, pressable_names
from corner.jev import JevError, ask
from corner.secret import read_key
from corner.model import (
    RECENT_EVENTS,
    app_from_activewindow,
    build_split_request,
    CORNER_DEBUG_CHORD,
    saved_rank,
    chord_id,
    chord_stats,
    commands_from_binds,
    seed_corner_debug,
    add_jev_spend,
    debug_line,
    history_document,
    jev_usage,
    load_history,
    needs_full_rank,
    parse_bind_keys,
    bar_label,
    layer_phrase,
    discovery_rows,
    rebind_rows,
    remember_action,
    remember_app,
    screen_state,
    slice_rows,
    used_layer_name,
    widget_at,
    spend_line,
    available_commands,
    kept_unused,
    note_unused,
    unused_from_shows,
    offer_rows,
    rotate_saved,
    row_chord_ids,
    suggestion_rows,
)
from corner.xkb import Keysyms

CONFIG_DIR = Path.home() / ".config" / "ignotas" / "shortcuts-ai"
STATE_DIR = Path.home() / ".local" / "state" / "omarchy" / "shortcuts-ai"
KEY_FILE = CONFIG_DIR / "jev.key"

SHOW_PX = 10
APPROACH_PX = 80
KEEP_W = 500
KEEP_H = 320
# After a jump the card stays up this long so the pointer does not close it.
# The corner file is written when the card disappears, not while it is still up.
PLACE_SAVE_SECONDS = 5
# Debug draws the last Jev input to the left of the card. The keep zone
# has to cover that wider card or the pointer loop closes it.
DEBUG_KEEP_W = 1280
CACHE_SECONDS = 60  # same request body is not sent again inside this window
POLL_SECONDS = 0.04


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    Corner().serve()


class Corner:
    def __init__(self) -> None:
        self.out_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.arrow_lock = threading.Lock()
        self.usage: list[dict] = []
        self.recent: deque[str] = deque(maxlen=RECENT_EVENTS)
        self.apps: list[str] = []
        self.rank: dict | None = None
        self._history_text = ""
        self.commands: list[dict] = []
        self.commands_at = 0.0
        self.monitors: list[dict] = []
        self.monitors_at = 0.0
        self.keys: Keysyms | None = None
        self.layout = "gb"
        self.result: dict | None = None
        self.card: dict | None = None  # last card with shortcuts; history.json stores this
        self.suggesting = False
        self.phase = "away"
        self.session = 0
        self.monitor = ""
        self.hook_ok = False
        self.hooks_reset = False
        self.debug = False
        self.last_input = ""
        self.mac_keys = mac_keyboard()
        self.armed = False
        self.arm_at = 0.0
        self.d_code: int | None = None
        self.pointer: tuple[float, float] | None = None
        self._panel_label = ""
        self._opened_name = ""
        # Not named _bar_slots: that is the method below. An instance
        # attribute with the method's name would hide it and crash the lookup.
        self.bar_geometry: list = []
        self.bar_geometry_at = 0.0
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.usage_path = STATE_DIR / "usage.jsonl"
        self.shows_path = STATE_DIR / "shown.json"
        self.unused_path = STATE_DIR / "unused.json"
        self.unused: dict = {}
        self._display_ids: list[str] = []
        self._used_now: set[str] = set()
        self.history_path = STATE_DIR / "history.json"
        self.spend_path = STATE_DIR / "jev.json"
        self.spend = _read_spend(self.spend_path)
        self.debug_path = STATE_DIR / "debug"
        self.debug = _read_flag(self.debug_path)
        self.dkey_path = STATE_DIR / "dkey.log"
        self.dkey_hook_path = STATE_DIR / "dkey.lua"
        self.arrow_path = STATE_DIR / "arrows.log"
        self.place_path = STATE_DIR / "corner"
        self.place = _read_place(self.place_path)
        self.place_pending = ""
        self.place_due = 0.0
        try:
            self.dkey_offset = self.dkey_path.stat().st_size
        except OSError:
            self.dkey_offset = 0
        try:
            self.arrow_offset = self.arrow_path.stat().st_size
        except OSError:
            self.arrow_offset = 0
        self.chord_path = STATE_DIR / "chords.log"
        self.offset_path = STATE_DIR / "chords.offset"
        self.hook_path = STATE_DIR / "hook.lua"
        self._load_usage()
        self._load_unused()
        self._load_history()
        self._seed_corner_debug()

    def serve(self) -> None:
        threading.Thread(target=self._chords, name="corner-chords", daemon=True).start()
        threading.Thread(target=self._events, name="corner-events", daemon=True).start()
        threading.Thread(target=self._pointer, name="corner-pointer", daemon=True).start()
        threading.Thread(target=self._arrows, name="corner-arrows", daemon=True).start()
        threading.Thread(target=self._hook_loop, name="corner-hook", daemon=True).start()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            op = request.get("op")
            if op == "ping":
                self.emit({"op": "pong"})
            elif op == "run":
                self.emit(self.run(str(request.get("arg") or "")))
            elif op == "suggest":
                self.kick_suggest()
            elif op == "away":
                # The card has left the screen. Save whatever corner it jumped to.
                self._hide_now()
            elif op == "debug":
                self.toggle_from_card()

    def _hide_now(self) -> None:
        with self.state_lock:
            if self.phase != "shown":
                return
            self.phase = "away"
            # Hide names the session on screen. The next open gets a new one.
            session = self.session
            self.session += 1
            # One tally for this visit. The file write stays under the lock so a
            # use cannot put the old counts back.
            ids = list(self._display_ids)
            used = set(self._used_now)
            self._display_ids = []
            self._used_now = set()
            if ids:
                self.unused = note_unused(self.unused, ids, used, time.time())
                _write_json(self.unused_path, self.unused)
            # The quiet window belongs to the open card. Closing it is done moving.
            pending = getattr(self, "place_pending", "")
            place = format_place(self.place) if pending else ""
            self.place_pending = ""
        if place and getattr(self, "place_path", None) is not None:
            fx, fy = normalize_place(place)
            if _write_text(self.place_path, place + "\n"):
                self.emit({"op": "place", "x": fx, "y": fy, "moving": False})
        self.emit({"op": "hide", "session": session})

    def emit(self, payload: dict) -> None:
        with self.out_lock:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    def run(self, arg: str) -> dict:
        if not arg.isdigit():
            return {"op": "ran", "ok": False, "note": "That shortcut is not a command."}
        script = (
            "local f = debug.getregistry()[%s];"
            "if type(f) ~= 'function' then return 'gone' end;"
            "local ok = pcall(f);"
            "if ok then return 'ok' end;"
            "return 'err'"
        ) % arg
        text = _repl(script)
        if text.strip() == "ok":
            self._mark_clicked(arg)
            # The card closes on a successful run. The move ends with it.
            self._hide_now()
            return {"op": "ran", "ok": True, "note": ""}
        return {"op": "ran", "ok": False, "note": "That shortcut changed. Come back to the corner."}

    def kick_suggest(self) -> None:
        with self.state_lock:
            if self.suggesting:
                return
            self.suggesting = True
        threading.Thread(target=self._suggest, name="corner-suggest", daemon=True).start()

    def _suggest(self) -> None:
        try:
            result = self._compute()
        except Exception:
            result = {
                "items": [],
                "note": "",
                "error": "The corner could not read the screen.",
                "fp": "",
                "at": time.time(),
            }
        with self.state_lock:
            self.result = result
            if result.get("items") and not result.get("error"):
                self.card = result
            self.suggesting = False
            shown = self.phase == "shown"
            session = self.session
            flush = bool(not result.get("error") and (result.get("fp") or self.card))
        if flush:
            self._flush_history()
        if shown:
            self._remember_display(result.get("items") or [])
            self._emit_card({
                "op": "update",
                "session": session,
                "items": result["items"],
                "note": result["note"],
                "error": result["error"],
                "spend": debug_line(self.debug, result.get("spend") or ""),
            })

    def _compute(self) -> dict:
        state = self._capture()
        commands = [command for command in self._commands() if isinstance(command, dict)]
        with self.state_lock:
            usage = list(self.usage)
            rank = self.rank
            cached = self.result
            card = self.card
        stats = chord_stats(usage, time.time())
        names = list(state.get("recent_apps") or [])
        unused = self._unused()
        now = time.time()
        if not needs_full_rank(rank, names, usage, now):
            return self._from_rank(rank, commands, names, unused, card, stats, state, now)
        request = build_split_request(state, commands, stats)
        self._remember_input(request)
        fingerprint = _fingerprint(request)
        now = time.time()
        if reuse_answer(cached, fingerprint, now) and isinstance(cached, dict) and cached.get("pool"):
            hit = self._reshuffle(dict(cached), commands, state, stats, unused, now)
            rebound = rebind_rows(hit.get("items") or [], commands)
            if rebound is not None:
                if hit.get("items") and not rebound:
                    hit = None
                else:
                    hit["items"] = rebound
            if hit is not None:
                hit["from_disk"] = False
                hit["at"] = now
                hit["spend"] = spend_line(self.spend, 0, hit.get("confidence"))
                hit["charged"] = 0
                return hit
        if not commands or not request.get("questions"):
            return _view([], fingerprint, note="No shortcuts to suggest.", spend=spend_line(self.spend, 0))
        key = _read_key()
        if not key:
            return _view(
                [], fingerprint,
                error="No Jev key in the keyring. Run shortcuts-ai-key.",
                spend=spend_line(self.spend, 0),
            )
        try:
            body = ask(key, request)
        except JevError as exc:
            # A refused key stays visible. Any other miss keeps the previous ranking.
            if exc.status != 401 and isinstance(rank, dict):
                previous = list(rank.get("apps") or [])
                return self._from_rank(rank, commands, previous, unused, card, stats, state, now)
            if exc.status != 401:
                held = held_card(card, commands, state, stats, fingerprint, now, unused)
                if held is not None:
                    held = self._reshuffle(held, commands, state, stats, unused, now)
                    held["spend"] = spend_line(self.spend, 0, held.get("confidence"))
                    return held
            return _view([], "", error=str(exc), spend=spend_line(self.spend, 0))
        charged = self._note_spend(body)
        saved = saved_rank(names, request["questions"], body["answers"], time.time())
        confidence = saved.get("confidence")
        rows, pool = slice_rows(
            commands, saved["probabilities"], names, unused, saved["explore"], now, stats,
            state.get("just_did"),
        )
        saved["offered"] = row_chord_ids(rows, commands)
        with self.state_lock:
            self.rank = saved
        if not rows:
            held = held_card(card, commands, state, stats, fingerprint, now, unused)
            if held is not None:
                held = self._reshuffle(held, commands, state, stats, unused, now)
                held["charged"] = charged
                held["spend"] = spend_line(self.spend, charged)
                return held
        return _view(
            rows, fingerprint,
            note="" if rows else "No clear shortcut.",
            confidence=confidence,
            charged=charged,
            spend=spend_line(self.spend, charged, confidence),
            pool=pool,
        )

    def _from_rank(
        self,
        rank: dict,
        commands: list,
        names: list,
        unused: dict,
        card: dict | None,
        stats: dict,
        state: dict,
        now: float,
    ) -> dict:
        """The saved maps are the only source. This visit does not call Jev."""
        probabilities = rank.get("probabilities") if isinstance(rank.get("probabilities"), dict) else {}
        explore = rank.get("explore") if "explore" in rank and isinstance(rank.get("explore"), dict) else None
        rows, pool = slice_rows(
            commands, probabilities, names, unused, explore, now, stats,
            state.get("just_did"),
        )
        fingerprint = _rank_fingerprint(names)
        if not rows:
            held = held_card(card, commands, state, stats, fingerprint, now, unused)
            if held is not None:
                held["spend"] = spend_line(self.spend, 0, rank.get("confidence"))
                held["charged"] = 0
                return held
        return _view(
            rows, fingerprint,
            note="" if rows else "No clear shortcut.",
            confidence=rank.get("confidence"),
            charged=0,
            spend=spend_line(self.spend, 0, rank.get("confidence")),
            pool=pool,
        )

    def _discovery_items(self) -> list[dict]:
        commands = self._commands()
        with self.state_lock:
            usage = list(self.usage)
        now = time.time()
        stats = chord_stats(usage, now)
        fresh = available_commands(commands, self._unused(), now, stats)
        return discovery_rows(fresh, self._capture(), stats)

    def _capture(self) -> dict:
        active = _hypr_json("activewindow")
        clients = _hypr_json("clients")
        if not isinstance(active, dict):
            active = None
        if not isinstance(clients, list):
            clients = []
        focused = ""
        if active:
            focused = str(active.get("class") or active.get("initialClass") or "")
        with self.state_lock:
            updated = remember_app(self.apps, focused)
            if updated != self.apps:
                self.apps = updated
            recent = list(self.recent)
            apps = list(self.apps)
        self._flush_history()
        return screen_state(active, clients, recent, apps, self._bar_slots())

    def _commands(self) -> list[dict]:
        now = time.time()
        with self.state_lock:
            if self.commands and now - self.commands_at < 15:
                return list(self.commands)
        binds = _hypr_json("binds")
        if not isinstance(binds, list):
            binds = []
        declared = []
        for path in _bind_files():
            try:
                declared.extend(parse_bind_keys(path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        keys = self._keysyms()
        commands = commands_from_binds(binds, declared, keys.name, pressable_names(keys))
        with self.state_lock:
            self.commands = commands
            self.commands_at = now
        return commands

    def _keysyms(self) -> Keysyms:
        if self.keys is None:
            layout = self.layout
            devices = _hypr_json("devices")
            if isinstance(devices, dict):
                for keyboard in devices.get("keyboards") or []:
                    if keyboard.get("layout"):
                        layout = str(keyboard["layout"])
                        break
            self.layout = layout
            self.keys = Keysyms(layout)
        return self.keys

    def _pointer(self) -> None:
        show_samples = 0
        while True:
            time.sleep(POLL_SECONDS)
            self._take_arrows()
            self._store_place_if_due()
            try:
                point = _cursor()
                monitors = self._monitors()
            except Exception:
                continue
            if point is None:
                continue
            with self.state_lock:
                self.pointer = point
                phase = self.phase
                shown_on = self.monitor
                keep_w = DEBUG_KEEP_W if self.debug else KEEP_W
                place = self.place
                ignore_cursor = self._moving_locked()
            if ignore_cursor:
                # The card is up, so the pointer must not close it mid-jump.
                # Once the card is gone this is false and the arrows are released.
                self._set_arm(True)
                self._take_dkey()
                continue
            zone, monitor = _zone(point[0], point[1], monitors, phase, shown_on, keep_w, place)
            with self.state_lock:
                if zone == "keep" and self.phase != "shown":
                    zone = "away"
            if zone in {"show", "approach", "keep"} and monitor:
                self.monitor = monitor
            # Same corner the card uses: the hotspot, and the card while it is open.
            self._set_arm(_arms_d(zone))
            self._take_dkey()
            if phase == "shown" and zone not in {"show", "approach", "keep"}:
                self._hide_now()
                continue
            if zone == "show":
                show_samples += 1
            else:
                show_samples = 0
            if zone == "approach" and phase == "away":
                with self.state_lock:
                    self.phase = "approach"
                    self.monitor = monitor
                self.kick_suggest()
            if zone == "show" and show_samples >= 2 and phase != "shown":
                with self.state_lock:
                    self.phase = "shown"
                    self.monitor = monitor
                    self.session += 1
                    session = self.session
                    result = self.result
                self.kick_suggest()
                discovered = False
                if isinstance(result, dict) and result.get("from_disk"):
                    fresh = self._discovery_items()
                    if fresh:
                        result = _view(
                            fresh, "",
                            spend=result.get("spend") or "",
                            from_disk=False,
                        )
                        discovered = True
                if isinstance(result, dict) and result.get("items"):
                    rebound = rebind_rows(result["items"], self._commands())
                    if rebound is not None:
                        if not rebound and result.get("from_disk"):
                            result = None
                        else:
                            result = dict(result)
                            result["items"] = rebound
                if isinstance(result, dict) and not discovered:
                    result = self._reshuffle_live(result)
                view = card_payload(result if isinstance(result, dict) else None, time.time())
                saved = isinstance(result, dict) and (not view["loading"] or result.get("from_disk"))
                spend_text = str(result.get("spend") or "") if saved and isinstance(result, dict) else ""
                self._remember_display(view["items"])
                self._emit_card({
                    "op": "show",
                    "session": session,
                    "monitor": monitor,
                    "loading": view["loading"],
                    "items": view["items"],
                    "note": view["note"],
                    "error": view["error"],
                    "spend": debug_line(self.debug, spend_text or spend_line(self.spend, 0)),
                })

    def _monitors(self) -> list[dict]:
        now = time.time()
        with self.state_lock:
            if self.monitors and now - self.monitors_at < 2:
                return list(self.monitors)
        parsed = _hypr_json("monitors")
        monitors = parsed if isinstance(parsed, list) else []
        with self.state_lock:
            self.monitors = monitors
            self.monitors_at = now
        return monitors

    def _chords(self) -> None:
        self.chord_path.touch()
        offset = _read_offset(self.offset_path)
        while True:
            try:
                size = self.chord_path.stat().st_size
                if offset > size:
                    offset = 0
                if size == offset:
                    if size > 65536:
                        self.chord_path.write_bytes(b"")
                        offset = 0
                        _write_offset(self.offset_path, 0)
                    time.sleep(0.2)
                    continue
                with self.chord_path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read()
                offset += len(data)
                _write_offset(self.offset_path, offset)
                for line in data.decode("utf-8", "replace").splitlines():
                    self._ingest_chord(line)
            except Exception:
                time.sleep(0.5)

    def _ingest_chord(self, line: str) -> None:
        parts = line.split()
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return
        keycode, mods, when = (int(part) for part in parts)
        try:
            name = self._keysyms().name(keycode)
        except Exception:
            return
        if not name:
            return
        ident = chord_id(mods, name)
        commands = self._commands()
        if not any(command["chord_id"] == ident for command in commands):
            return
        self._remember_chord(ident, float(when))

    def _events(self) -> None:
        while True:
            path = _event_socket()
            if path is None:
                time.sleep(1)
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(str(path))
                    pending = b""
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            try:
                                self._on_event(line.decode("utf-8", "replace"))
                            except Exception:
                                # One bad event must not drop the socket.
                                continue
            except Exception:
                time.sleep(1)

    def _on_event(self, line: str) -> None:
        name, _, data = line.partition(">>")
        text = ""
        if name == "activewindow":
            app = app_from_activewindow(data)
            text = f"focused {app}" if app else ""
        elif name == "openwindow":
            parts = data.split(",", 3)
            text = "opened " + (parts[2] if len(parts) > 2 else "a window")
        elif name == "closewindow":
            text = "closed a window"
        elif name == "workspace":
            text = "workspace " + data.strip()
        elif name == "fullscreen":
            text = "fullscreen on" if data.strip() not in {"", "0"} else "fullscreen off"
        elif name == "changefloatingmode":
            text = "floated a window" if data.rsplit(",", 1)[-1].strip() == "1" else "tiled a window"
        elif name in {"openlayer", "closelayer"}:
            text = self._layer_event(name, data)
        elif "config" in name:
            with self.state_lock:
                self.hook_ok = False
                self.hooks_reset = True
                self.commands_at = 0
                self.armed = False
                self.arm_at = 0.0
        if not text:
            return
        with self.state_lock:
            if not remember_action(self.recent, text):
                return
            if name == "activewindow":
                self.apps = remember_app(self.apps, app_from_activewindow(data))
            elif name == "openlayer" and self._opened_name:
                self.apps = remember_app(self.apps, self._opened_name)
                self._opened_name = ""
        self._flush_history()

    def _layer_event(self, name: str, data: str) -> str:
        """A bar popup opening is one short line. The widget is whichever button the pointer is on."""
        action = "opened" if name == "openlayer" else "closed"
        namespace = data.split(",", 1)[0].strip()
        widget = ""
        if namespace == "omarchy-keyboard-panel" and action == "opened":
            with self.state_lock:
                point = self.pointer
            if point is not None:
                widget = widget_at(self._bar_slots(), point[0], point[1])
            self._panel_label = bar_label(widget) if widget else "a panel"
        if action == "opened":
            self._opened_name = used_layer_name(namespace, widget)
        if namespace != "omarchy-keyboard-panel":
            return layer_phrase(action, namespace)
        if action == "closed":
            label = self._panel_label or "a panel"
            self._panel_label = ""
            return f"closed {label}"
        return layer_phrase(action, namespace, widget)

    def _bar_slots(self) -> list:
        """Widget boxes from the shell. Cached; a click must not become a Jev call."""
        now = time.time()
        if self.bar_geometry and now - self.bar_geometry_at < 30:
            return self.bar_geometry
        try:
            raw = subprocess.check_output(
                ["omarchy-shell", "shell", "debugBarGeometry"],
                text=True,
                timeout=0.6,
                stderr=subprocess.DEVNULL,
            )
            parsed = json.loads(raw)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return self.bar_geometry
        if not isinstance(parsed, list):
            return self.bar_geometry
        self.bar_geometry = parsed
        self.bar_geometry_at = now
        return self.bar_geometry

    def _hook_loop(self) -> None:
        while True:
            with self.state_lock:
                needed = not self.hook_ok
            if needed:
                self._install_hook()
            # A reload wipes the bind. Wake often enough to put it back,
            # and only call hyprctl when it is actually missing.
            time.sleep(1)

    def _install_hook(self) -> None:
        try:
            with self.state_lock:
                reset = self.hooks_reset
                self.hooks_reset = False
            if reset:
                _repl(_disarm_lua(clear_hook=True))
                self.armed = False
                self.arm_at = 0.0
            self.hook_path.write_text(hook_source(str(self.chord_path)), encoding="utf-8")
            text = _repl(f'dofile("{self.hook_path}")')
            code = self._d_code()
            if code is None:
                return
            self.dkey_hook_path.write_text(
                dkey_source(str(self.dkey_path), code, str(self.arrow_path)),
                encoding="utf-8",
            )
            # Drop the old swallow first. "already" would keep the previous binds.
            _repl(_disarm_lua(clear_hook=False))
            dtext = _repl(f'dofile("{self.dkey_hook_path}")')
        except Exception:
            return
        if ("installed" in text or "already" in text) and ("installed" in dtext or "already" in dtext):
            # A previous process may have left D bound. This one has not
            # seen the pointer yet, so start disarmed.
            _repl(
                "if _G.ignotas_shortcuts_ai_set_arm then "
                "return _G.ignotas_shortcuts_ai_set_arm(false) end return 'missing'"
            )
            self.armed = False
            self.arm_at = 0.0
            with self.state_lock:
                self.hook_ok = True

    def _d_code(self) -> int | None:
        if self.d_code is not None:
            return self.d_code
        try:
            keys = self._keysyms()
        except Exception:
            return None
        for code in range(8, 256):
            if keys.name(code).casefold() == "d":
                self.d_code = code
                return code
        return None

    def _set_arm(self, on: bool) -> None:
        if self.armed == on:
            return
        now = time.time()
        # Retry a missing hook slowly. Leaving the corner must drop the
        # bind on the next sample, or D keeps getting eaten outside it.
        if on and now - self.arm_at < 0.4:
            return
        word = "true" if on else "false"
        text = _repl(
            "if _G.ignotas_shortcuts_ai_set_arm then "
            f"return _G.ignotas_shortcuts_ai_set_arm({word}) "
            "end return 'missing'"
        )
        if "missing" in text or "failed" in text:
            self.arm_at = now
            return
        # "unbound" contains "bound". Either word means the call landed.
        if "bound" in text or "same" in text:
            self.armed = on
            self.arm_at = 0.0

    def _take_dkey(self) -> None:
        try:
            size = self.dkey_path.stat().st_size
        except OSError:
            return
        if size < self.dkey_offset:
            self.dkey_offset = 0
        if size == self.dkey_offset:
            return
        with self.dkey_path.open("rb") as handle:
            handle.seek(self.dkey_offset)
            data = handle.read()
        self.dkey_offset += len(data)
        for _ in range(data.count(b"\n")):
            self._on_corner_d()
        if self.dkey_offset >= size and self.dkey_offset > 4096:
            try:
                self.dkey_path.write_bytes(b"")
                self.dkey_offset = 0
            except OSError:
                return

    def _arrows(self) -> None:
        """Jump as soon as a key appends a line. The pointer poll can sit
        inside a cursor read for a third of a second, and the card must not wait."""
        watch = _AppendWatch(self.arrow_path)
        try:
            while True:
                self._take_arrows()
                watch.wait(0.2)
        finally:
            watch.close()

    def _take_arrows(self) -> None:
        with self.arrow_lock:
            try:
                size = self.arrow_path.stat().st_size
            except OSError:
                return
            if size < self.arrow_offset:
                self.arrow_offset = 0
            if size == self.arrow_offset:
                return
            with self.arrow_path.open("rb") as handle:
                handle.seek(self.arrow_offset)
                data = handle.read()
            self.arrow_offset += len(data)
            if self.arrow_offset >= size and self.arrow_offset > 4096:
                try:
                    self.arrow_path.write_bytes(b"")
                    self.arrow_offset = 0
                except OSError:
                    pass
        for line in data.decode("utf-8", "replace").splitlines():
            self._on_arrow(line.strip())

    def _walk_span(self) -> tuple[float, float]:
        """Logical width and height of the monitor the card is on."""
        monitors = getattr(self, "monitors", None) or []
        wanted = str(getattr(self, "monitor", "") or "")
        chosen = None
        for monitor in monitors:
            if not wanted or str(monitor.get("name") or "") == wanted:
                chosen = monitor
                break
        if chosen is None and monitors:
            chosen = monitors[0]
        if chosen is not None:
            box = _layout_box(chosen)
            if box is not None and box[2] > 0 and box[3] > 0:
                return box[2], box[3]
        return 1280.0, 800.0

    def _on_arrow(self, direction: str) -> None:
        width, height = self._walk_span()
        with self.state_lock:
            if self.phase != "shown":
                return
            moved = walk_place(self.place, direction, width, height)
            if moved is None:
                return
            self.place = moved
            self.place_pending = format_place(moved)
            self.place_due = time.time() + PLACE_SAVE_SECONDS
            session = self.session
            monitor = self.monitor
        fx, fy = moved
        self.emit({
            "op": "place",
            "x": fx,
            "y": fy,
            "moving": True,
            "session": session,
            "monitor": monitor,
        })

    def _moving_locked(self) -> bool:
        # Quiet time holds the card only while that card is on screen.
        return getattr(self, "phase", "") == "shown" and bool(self.place_pending) and time.time() < self.place_due

    def _moving(self) -> bool:
        with self.state_lock:
            return self._moving_locked()

    def _store_place_if_due(self) -> None:
        """The quiet wait only lets the card close. The file is written on that close."""
        now = time.time()
        with self.state_lock:
            if getattr(self, "phase", "") != "shown":
                return
            pending = getattr(self, "place_pending", "")
            due = getattr(self, "place_due", 0.0)
            if not pending or due == 0 or now < due:
                return
            self.place_due = 0.0
            fx, fy = normalize_place(self.place)
            session = self.session
            monitor = self.monitor
        self.emit({
            "op": "place",
            "x": fx,
            "y": fy,
            "moving": False,
            "session": session,
            "monitor": monitor,
        })

    def _on_corner_d(self) -> None:
        with self.state_lock:
            monitor = self.monitor
        self._remember_chord(CORNER_DEBUG_CHORD)
        self.emit({"op": "chomp", "monitor": monitor})
        self._toggle_debug()

    def _remember_chord(self, ident: str, when: float | None = None) -> None:
        event = {"t": time.time() if when is None else when, "chord": ident}
        with self.state_lock:
            self.usage.append(event)
        _append_usage(self.usage_path, event)
        self._mark_used(ident)

    def _seed_corner_debug(self) -> None:
        try:
            when = self.dkey_path.stat().st_mtime
            presses = self.dkey_path.read_text(encoding="utf-8", errors="replace").count("\n")
        except OSError:
            return
        seeded = seed_corner_debug(self.usage, presses, when, time.time())
        if len(seeded) == len(self.usage):
            return
        self.usage = seeded
        _write_usage(self.usage_path, seeded)

    def toggle_from_card(self) -> None:
        """The tiny D on the debug pane. A click is not a key press."""
        self._toggle_debug()

    def _toggle_debug(self) -> None:
        with self.state_lock:
            self.debug = not self.debug
            enabled = self.debug
            phase = self.phase
            session = self.session
            result = dict(self.result or {})
        _write_flag(self.debug_path, enabled)
        if phase != "shown":
            return
        self._emit_card({
            "op": "update",
            "session": session,
            "items": result.get("items") or [],
            "note": result.get("note") or "",
            "error": result.get("error") or "",
            "spend": debug_line(enabled, result.get("spend") or spend_line(self.spend, 0)),
        })

    def _remember_input(self, request: dict) -> None:
        text = format_debug_input(request)
        with self.state_lock:
            self.last_input = text

    def _emit_card(self, payload: dict) -> None:
        """Show and update carry the debug pane. The key never enters this payload."""
        with self.state_lock:
            payload["input"] = self.last_input if self.debug else ""
            payload["mac"] = self.mac_keys
            fx, fy = normalize_place(getattr(self, "place", (1.0, 1.0)))
            payload["x"] = fx
            payload["y"] = fy
            payload["moving"] = self._moving_locked()
        self.emit(payload)

    def _note_spend(self, body: dict) -> int:
        incoming, outgoing = jev_usage(body)
        with self.state_lock:
            self.spend = add_jev_spend(self.spend, incoming, outgoing)
            snapshot = dict(self.spend)
        _write_json(self.spend_path, snapshot)
        return incoming

    def _load_history(self) -> None:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        loaded = load_history(data)
        self.apps = list(loaded["apps"])
        self.rank = loaded.get("rank")
        self.recent = deque(loaded["just_did"], maxlen=RECENT_EVENTS)
        result = loaded["result"]
        if isinstance(result, dict):
            result["spend"] = spend_line(self.spend, 0, result.get("confidence"))
            result["charged"] = 0
            self.result = result
            if result.get("items"):
                self.card = result
        self._history_text = json.dumps(
            history_document(self.apps, self.recent, self.card, self.rank),
            separators=(",", ":"),
        ) + "\n"

    def _flush_history(self) -> None:
        """Write the compact history. The lock is held so two events cannot swap the file."""
        with self.state_lock:
            text = json.dumps(
                history_document(self.apps, self.recent, self.card, self.rank),
                separators=(",", ":"),
            ) + "\n"
            if text == self._history_text:
                return
            if not _write_text(self.history_path, text):
                return
            self._history_text = text

    def _load_usage(self) -> None:
        if not self.usage_path.exists():
            return
        rows = []
        for line in self.usage_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or not row.get("chord"):
                continue
            try:
                when = float(row.get("t") or 0)
            except (TypeError, ValueError):
                continue
            rows.append({"t": when, "chord": str(row["chord"])})
        self.usage = rows

    def _load_unused(self) -> None:
        try:
            data = json.loads(self.unused_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            self.unused = kept_unused(data)
            return
        try:
            old = json.loads(self.shows_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        migrated = unused_from_shows(old, time.time())
        if not migrated:
            return
        self.unused = migrated
        _write_json(self.unused_path, migrated)

    def _unused(self) -> dict:
        with self.state_lock:
            return dict(self.unused)

    def _mark_used(self, ident: str) -> None:
        """A press or a clicked row clears the ignored-display penalty."""
        if not ident:
            return
        with self.state_lock:
            self._used_now.add(ident)
            if ident not in self.unused:
                return
            self.unused.pop(ident, None)
            _write_json(self.unused_path, self.unused)

    def _mark_clicked(self, arg: str) -> None:
        """The row ran. It does not count as a key press."""
        for command in self._commands():
            if str(command.get("arg") or "") != arg:
                continue
            self._mark_used(str(command.get("chord_id") or ""))
            return

    def _remember_display(self, items: list) -> None:
        """Rows painted for this visit. A prefetch while the card is closed is ignored."""
        ids = row_chord_ids(items, self._commands())
        if not ids:
            return
        with self.state_lock:
            if self.phase != "shown":
                return
            self._display_ids = ids

    def _reshuffle(self, result: dict, commands: list, state: dict, stats: dict, unused: dict, now: float) -> dict:
        """Apply the ignore penalty. The request body is unchanged, so the cache still hits."""
        pool = result.get("pool")
        if isinstance(pool, list) and pool:
            rows = suggestion_rows(offer_rows(pool, unused, now=now, stats=stats))
        else:
            rows = rotate_saved(result.get("items") or [], commands, unused, state, stats, now=now)
        if not rows:
            return result
        hit = dict(result)
        hit["items"] = rows
        return hit

    def _reshuffle_live(self, result: dict) -> dict:
        pool = result.get("pool")
        unused = self._unused()
        now = time.time()
        with self.state_lock:
            usage = list(self.usage)
        stats = chord_stats(usage, now)
        if isinstance(pool, list) and pool:
            return self._reshuffle(result, [], {}, stats, unused, now)
        commands = self._commands()
        return self._reshuffle(result, commands, self._capture(), stats, unused, now)


def format_debug_input(request: dict) -> str:
    """Pretty JSON for the debug pane. Each criteria shortcut stays on one line.

    Display only. The body sent to Jev is the request dict, unchanged.
    """
    return _debug_json(request, 0)


def _debug_json(value: object, indent: int) -> str:
    if isinstance(value, dict):
        if not value:
            return "{}"
        pad = "  " * indent
        inner = "  " * (indent + 1)
        lines = []
        for key, item in value.items():
            if key == "criteria" and isinstance(item, dict):
                rendered = _debug_criteria(item, indent + 1)
            else:
                rendered = _debug_json(item, indent + 1)
            lines.append(f"{inner}{json.dumps(key, ensure_ascii=False)}: {rendered}")
        return "{\n" + ",\n".join(lines) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        pad = "  " * indent
        inner = "  " * (indent + 1)
        lines = [f"{inner}{_debug_json(item, indent + 1)}" for item in value]
        return "[\n" + ",\n".join(lines) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def _debug_criteria(criteria: dict, indent: int) -> str:
    """One line per shortcut. A nested value falls back to the normal layout."""
    if not criteria:
        return "{}"
    pad = "  " * indent
    inner = "  " * (indent + 1)
    lines = []
    for key, fact in criteria.items():
        flat = isinstance(fact, dict) and all(
            not isinstance(item, (dict, list)) for item in fact.values()
        )
        rendered = json.dumps(fact, ensure_ascii=False) if flat else _debug_json(fact, indent + 1)
        lines.append(f"{inner}{json.dumps(key, ensure_ascii=False)}: {rendered}")
    return "{\n" + ",\n".join(lines) + "\n" + pad + "}"


def _fingerprint(request: dict) -> str:
    """Cache key for one system-one body, including the use counts Jev sees."""
    raw = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _rank_fingerprint(names: list) -> str:
    """Stable id for a saved ranking. Order does not matter."""
    labels = sorted(str(name).casefold() for name in names if str(name).strip())
    raw = json.dumps(labels, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def reuse_answer(cached: dict | None, fingerprint: str, now: float) -> bool:
    """Same body is free for CACHE_SECONDS. A disk card matches even when older."""
    if not isinstance(cached, dict) or cached.get("error"):
        return False
    if str(cached.get("fp") or "") != fingerprint:
        return False
    if cached.get("from_disk"):
        return True
    try:
        at = float(cached.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return now - at < CACHE_SECONDS


def _view(items, fingerprint: str, **fields) -> dict:
    row = {
        "items": items,
        "note": "",
        "error": "",
        "fp": fingerprint,
        "at": time.time(),
        "charged": 0,
    }
    row.update(fields)
    return row


def held_card(
    card: dict | None,
    commands: list,
    state: dict,
    stats: dict | None,
    fingerprint: str,
    now: float,
    unused: dict | None = None,
) -> dict | None:
    """Unused shortcuts when Jev did not pick, otherwise the previous card."""
    rows = discovery_rows(available_commands(commands, unused, now, stats), state, stats)
    if rows:
        return _view(rows, fingerprint, at=now, from_disk=False)
    return offline_card(card, commands, fingerprint, now)


def offline_card(card: dict | None, commands: list, fingerprint: str, now: float) -> dict | None:
    """Last shortcuts when TypeSafe did not produce a card. None if nothing was saved."""
    if not isinstance(card, dict) or card.get("error"):
        return None
    items = card.get("items")
    if not isinstance(items, list) or not items:
        return None
    hit = dict(card)
    rebound = rebind_rows(items, commands)
    if rebound:
        hit["items"] = rebound
    hit["error"] = ""
    hit["note"] = ""
    hit["fp"] = fingerprint
    hit["at"] = now
    hit["from_disk"] = False
    hit["charged"] = 0
    return hit


def card_payload(result: dict | None, now: float) -> dict:
    """Rows to paint. A card with shortcuts stays up while a refresh runs."""
    ready = result if isinstance(result, dict) and result.get("items") is not None else None
    try:
        at = float(ready.get("at") or 0) if ready else 0.0
    except (TypeError, ValueError):
        at = 0.0
    fresh = bool(ready and now - at < CACHE_SECONDS)
    has_rows = bool(ready and ready.get("items") and not ready.get("error"))
    # A saved card stays on screen after a reboot, and an old card stays up
    # when the next call has not replaced it.
    keep = bool(ready and (fresh or ready.get("from_disk") or has_rows))
    return {
        "loading": not keep,
        "items": list(ready["items"]) if keep else [],
        "note": str(ready.get("note") or "") if keep else "",
        "error": str(ready.get("error") or "") if keep and not has_rows else "",
    }


def _write_text(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        return False
    return True


def _read_key() -> str:
    return read_key(KEY_FILE)


def _arms_d(zone: str) -> bool:
    """Pac-Man eats D in the hotspot and while the pointer is still on the open card.

    Approach only prefetches. Away is the rest of the screen.
    """
    return zone in {"show", "keep"}


def _corner_axis(value: float) -> float:
    """0 or 1. Anything in between snaps to the nearer screen edge."""
    return 1.0 if float(value) >= 0.5 else 0.0


def normalize_place(text) -> tuple[float, float]:
    """One of the four corners. 1, 1 is the bottom-right. Never the middle."""
    if isinstance(text, tuple) and len(text) == 2:
        try:
            return _corner_axis(text[0]), _corner_axis(text[1])
        except (TypeError, ValueError):
            return 1.0, 1.0
    raw = " ".join(str(text or "").casefold().split())
    named = {
        "bottom-right": (1.0, 1.0),
        "bottom-left": (0.0, 1.0),
        "top-right": (1.0, 0.0),
        "top-left": (0.0, 0.0),
    }
    if raw in named:
        return named[raw]
    parts = raw.split()
    if len(parts) == 2:
        try:
            return _corner_axis(float(parts[0])), _corner_axis(float(parts[1]))
        except ValueError:
            pass
    return 1.0, 1.0


def format_place(place) -> str:
    fx, fy = normalize_place(place)
    vertical = "bottom" if fy == 1.0 else "top"
    horizontal = "right" if fx == 1.0 else "left"
    return f"{vertical}-{horizontal}"


def walk_place(
    place,
    direction: str,
    width: float = 1280,
    height: float = 800,
    step: float = 0,
) -> tuple[float, float] | None:
    """The next corner in that direction, or None when already on that edge."""
    del width, height, step
    fx, fy = normalize_place(place)
    arrow = str(direction or "").casefold()
    if arrow == "left":
        moved = (0.0, fy)
    elif arrow == "right":
        moved = (1.0, fy)
    elif arrow == "up":
        moved = (fx, 0.0)
    elif arrow == "down":
        moved = (fx, 1.0)
    else:
        return None
    if moved == (fx, fy):
        return None
    return moved


def place_ready(
    pending: str | None,
    saved: str,
    now: float,
    at: float,
    wait: float = PLACE_SAVE_SECONDS,
) -> str | None:
    """The corner to write, once the arrows have been still for `wait` seconds."""
    if not pending or pending == saved:
        return None
    if now < at + wait:
        return None
    return pending


def _read_place(path: Path) -> str:
    try:
        return normalize_place(path.read_text(encoding="utf-8"))
    except OSError:
        return "bottom-right"


def _zone(
    x: float,
    y: float,
    monitors: list[dict],
    phase: str,
    shown_on: str,
    keep_w: float = KEEP_W,
    place="bottom-right",
) -> tuple[str, str]:
    fx, fy = normalize_place(place)
    for monitor in monitors:
        box = _layout_box(monitor)
        if box is None:
            continue
        left, top, width, height = box
        if not (left <= x < left + width and top <= y < top + height):
            continue
        name = str(monitor.get("name") or "")
        hx = left + fx * width
        hy = top + fy * height
        dx = abs(x - hx)
        dy = abs(y - hy)
        if dx <= SHOW_PX and dy <= SHOW_PX:
            return "show", name
        if dx <= APPROACH_PX and dy <= APPROACH_PX:
            return "approach", name
        if phase == "shown" and name == shown_on:
            keep_left = hx - fx * keep_w
            keep_top = hy - fy * KEEP_H
            if keep_left <= x < keep_left + keep_w and keep_top <= y < keep_top + KEEP_H:
                return "keep", name
        return "away", name
    return "away", ""


def _layout_box(monitor: dict) -> tuple[float, float, float, float] | None:
    try:
        scale = float(monitor.get("scale") or 1) or 1
        width = float(monitor["width"]) / scale
        height = float(monitor["height"]) / scale
        if int(monitor.get("transform") or 0) % 2 == 1:
            width, height = height, width
        return float(monitor["x"]), float(monitor["y"]), width, height
    except (KeyError, TypeError, ValueError):
        return None


_IN_NONBLOCK = 0x800
_IN_CLOEXEC = 0x80000
_IN_MODIFY = 0x2
_IN_CLOSE_WRITE = 0x8


class _AppendWatch:
    """Block until a file is appended. A missing inotify sleeps a frame instead."""

    def __init__(self, path: Path) -> None:
        self._fd = -1
        self._poll: select.poll | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            lib = ctypes.CDLL(None, use_errno=True)
            lib.inotify_init1.argtypes = [ctypes.c_int]
            lib.inotify_init1.restype = ctypes.c_int
            lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            lib.inotify_add_watch.restype = ctypes.c_int
            fd = lib.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
            if fd < 0:
                return
            self._fd = fd
            mask = _IN_MODIFY | _IN_CLOSE_WRITE
            if lib.inotify_add_watch(fd, os.fsencode(path), mask) < 0:
                self.close()
                return
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            self._poll = poll
        except (OSError, AttributeError):
            self.close()

    def wait(self, seconds: float) -> None:
        poll = self._poll
        if poll is None or self._fd < 0:
            time.sleep(0.01)
            return
        if not poll.poll(max(0, int(seconds * 1000))):
            return
        try:
            while True:
                chunk = os.read(self._fd, 65536)
                if len(chunk) < 65536:
                    break
        except (BlockingIOError, OSError):
            return

    def close(self) -> None:
        fd = self._fd
        self._fd = -1
        self._poll = None
        if fd >= 0:
            os.close(fd)


def _cursor() -> tuple[float, float] | None:
    path = _request_socket()
    if path is None:
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        sock.connect(str(path))
        sock.sendall(b"cursorpos")
        data = sock.recv(64).decode("utf-8", "replace")
    x_text, y_text = data.split(",", 1)
    return float(x_text), float(y_text)


def _hypr_dir() -> Path | None:
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not signature:
        return None
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "hypr" / signature


def _hypr_socket(name: str) -> Path | None:
    root = _hypr_dir()
    if root is None:
        return None
    path = root / name
    return path if path.exists() else None


def _request_socket() -> Path | None:
    return _hypr_socket(".socket.sock")


def _event_socket() -> Path | None:
    return _hypr_socket(".socket2.sock")


def _hyprctl(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["hyprctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _hypr_json(command: str):
    completed = _hyprctl(["-j", command], 2)
    if completed is None:
        return None
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        return None


def _repl(code: str) -> str:
    completed = _hyprctl(["repl", code], 3)
    if completed is None:
        return ""
    return (completed.stdout or "") + (completed.stderr or "")


def _disarm_lua(clear_hook: bool) -> str:
    """Unbind D before a new script is loaded. Never call hl.unbind("D")."""
    hook = "_G.ignotas_shortcuts_ai_hook = nil; " if clear_hook else ""
    return (
        "if _G.ignotas_shortcuts_ai_set_arm then _G.ignotas_shortcuts_ai_set_arm(false) end; "
        + hook
        + "_G.ignotas_shortcuts_ai_dkey = nil; "
        "_G.ignotas_shortcuts_ai_set_arm = nil; "
        "return 'cleared'"
    )


def _bind_files() -> list[Path]:
    omarchy = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
    user = Path.home() / ".config" / "hypr"
    files = sorted(omarchy.glob("default/hypr/**/*.lua"))
    files.extend(sorted(user.glob("**/*.lua")))
    return files


def _read_offset(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_offset(path: Path, offset: int) -> None:
    path.write_text(str(offset), encoding="utf-8")


def _read_flag(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def _write_flag(path: Path, on: bool) -> None:
    _write_text(path, "1\n" if on else "0\n")


def _read_spend(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0}
    if not isinstance(data, dict):
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "usd": 0.0}
    return data


def _write_json(path: Path, data: dict) -> None:
    _write_text(path, json.dumps(data, separators=(",", ":")) + "\n")


def _append_usage(path: Path, row: dict) -> None:
    """One press, kept. The file is not trimmed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        return


def _write_usage(path: Path, rows: list[dict]) -> None:
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
