"""Decisions that do not need Hyprland, the network, or the API key."""

import json
import re
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from corner.daemon import (
    DEBUG_KEEP_W,
    Corner,
    _arms_d,
    _fingerprint,
    _layout_box,
    _zone,
    card_payload,
    format_debug_input,
    held_card,
    offline_card,
    reuse_answer,
)
from corner.hook import dkey_source, hook_source
from corner.jev import ranking_ok
from corner.keyboard import apple_hardware, evdev_codes, key_bits
from corner.secret import clear, lookup, read_key, store
from corner.model import (
    FUNCTION_KEYCODE,
    SHOW_CONFIDENCE,
    build_request,
    build_split_request,
    chord_id,
    chord_stats,
    commands_from_binds,
    CORNER_DEBUG_CHORD,
    learned_chords,
    seed_corner_debug,
    parse_bind_keys,
    discovery_rows,
    rank,
    UNUSED_FORGET_SECONDS,
    UNUSED_SHOWS,
    rebind_rows,
    app_from_activewindow,
    cap_choices,
    history_document,
    bar_apps,
    layer_phrase,
    matches_used,
    needs_full_rank,
    same_used,
    slice_rows,
    used_layer_name,
    load_history,
    remember_action,
    remember_app,
    saved_rank,
    screen_state,
    shortlist,
    available_commands,
    note_unused,
    unused_penalty,
    offer_rows,
    rotate_saved,
    suggest_rows,
    widget_at,
    add_jev_spend,
    debug_line,
    format_usd,
    jev_cost,
    jev_usage,
    spend_line,
    suggestion_rows,
)

ROOT = Path(__file__).resolve().parents[1]
OMARCHY = Path("/usr/share/omarchy/default/hypr")


class LearnedTests(unittest.TestCase):
    def test_six_presses_in_a_day_is_learned(self):
        now = 1_000_000.0
        events = [{"t": now - 10, "chord": "64:return"} for _ in range(6)]
        events.append({"t": now - 10, "chord": "64:c"})
        events.append({"t": now - 90000, "chord": "64:w"})
        self.assertEqual(learned_chords(events, now), {"64:return"})

    def test_five_is_not_enough(self):
        now = 1_000_000.0
        events = [{"t": now - 1, "chord": "64:return"} for _ in range(5)]
        self.assertEqual(learned_chords(events, now), set())

    def test_stats_keep_uses_hour_and_a_coarse_age(self):
        now = 1_000_000.0
        events = [{"t": now - 30, "chord": "64:return"} for _ in range(5)]
        events.append({"t": now - 2 * 3600, "chord": "64:return"})
        events.append({"t": now - 10, "chord": "64:c"})
        events.append({"t": now - 90000, "chord": "64:w"})
        stats = chord_stats(events, now)
        self.assertEqual(stats["64:return"], {"uses": 6, "ago": 0, "hour": 5})
        self.assertEqual(stats["64:c"], {"uses": 1, "ago": 0, "hour": 1})
        self.assertEqual(stats["64:w"], {"uses": 1, "ago": 1440})
        older = chord_stats([{"t": now - 2 * 3600, "chord": "64:w"}], now)
        self.assertEqual(older["64:w"], {"uses": 1, "ago": 60})
        self.assertEqual(chord_stats([{"t": now - 5 * 60, "chord": "a"}], now)["a"]["ago"], 5)
        self.assertEqual(chord_stats([{"t": now - 5 * 60 + 1, "chord": "a"}], now)["a"]["ago"], 0)


class ScreenTests(unittest.TestCase):
    def test_state_is_small_and_has_no_pixels(self):
        active = {
            "class": "chromium",
            "title": "A" * 200,
            "floating": False,
            "fullscreen": 0,
            "workspace": {"id": 1, "name": "1"},
            "mapped": True,
        }
        other = {
            "class": "Alacritty",
            "title": "shell",
            "mapped": True,
            "hidden": False,
            "workspace": {"id": 1, "name": "1"},
        }
        hidden = {"class": "secret", "title": "nope", "mapped": True, "hidden": True, "workspace": {"id": 1}}
        state = screen_state(active, [active, other, hidden], ["focused chromium"])
        self.assertLessEqual(len(state["focused"]["title"]), 48)
        self.assertEqual(state["also_visible"], [{"app": "Alacritty", "title": "shell"}])
        self.assertEqual(state["recent_apps"], ["chromium"])
        self.assertNotIn("address", json.dumps(state))
        raw = json.dumps(state)
        self.assertLess(len(raw), 800)


class BarClickTests(unittest.TestCase):
    def test_the_wifi_button_is_the_widget_under_the_pointer(self):
        slots = [
            {"id": "omarchy.network", "x": 1164, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.audio", "x": 1191, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.tray", "x": 1113, "y": 0, "width": 0, "height": 0, "visible": False},
        ]
        self.assertEqual(widget_at(slots, 1170, 10), "omarchy.network")
        self.assertEqual(widget_at(slots, 1200, 10), "omarchy.audio")
        self.assertEqual(widget_at(slots, 400, 400), "")

    def test_a_bar_popup_is_one_short_line(self):
        self.assertEqual(layer_phrase("opened", "omarchy-keyboard-panel", "omarchy.network"), "opened wifi")
        self.assertEqual(layer_phrase("opened", "omarchy-keyboard-panel", "omarchy.clock"), "opened calendar")
        self.assertEqual(layer_phrase("closed", "omarchy-keyboard-panel", "omarchy.network"), "closed wifi")
        self.assertEqual(layer_phrase("opened", "omarchy-keyboard-panel", ""), "opened a panel")
        self.assertEqual(layer_phrase("opened", "omarchy-menu"), "opened menu")
        self.assertEqual(layer_phrase("opened", "omarchy-bar"), "")
        self.assertEqual(layer_phrase("opened", "omarchy-osd"), "")
        self.assertEqual(layer_phrase("opened", "ignotas-shortcuts-ai"), "")
        phrase = layer_phrase("opened", "omarchy-keyboard-panel", "omarchy.network")
        self.assertLessEqual(len(phrase), 40)

    def test_a_spinning_title_does_not_erase_the_bar(self):
        recent = deque(maxlen=8)
        remember_action(recent, "opened wifi")
        remember_action(recent, "opened calendar")
        self.assertTrue(remember_action(recent, "focused Alacritty"))
        for _ in range(12):
            self.assertFalse(remember_action(recent, "focused Alacritty"))
        self.assertEqual(list(recent), ["opened wifi", "opened calendar", "focused Alacritty"])

    def test_every_visible_bar_widget_is_read(self):
        slots = [
            {"id": "omarchy.menu", "x": 8, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.clock", "x": 585, "y": 0, "width": 111, "height": 26, "visible": True},
            {"id": "omarchy.weather", "x": 696, "y": 0, "width": 21, "height": 26, "visible": True},
            {"id": "njpatel.omabot", "x": 1113, "y": 0, "width": 24, "height": 26, "visible": True},
            {"id": "omarchy.bluetooth", "x": 1137, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.network", "x": 1164, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.audio", "x": 1191, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.monitor", "x": 1218, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.power", "x": 1245, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.tray", "x": 1113, "y": 0, "width": 0, "height": 0, "visible": False},
            {"id": "omarchy.agents", "x": 1100, "y": 0, "width": 0, "height": 0, "visible": False},
        ]
        self.assertEqual(
            bar_apps(slots),
            ["menu", "calendar", "weather", "omabot", "bluetooth", "wifi", "audio", "display", "power"],
        )
        state = screen_state(None, [], [], None, slots)
        self.assertEqual(state["bar"], bar_apps(slots))
        filler = [
            {"id": str(i), "arg": str(i), "description": name, "chord": name, "chord_id": f"f:{i}"}
            for i, name in enumerate([
                "Terminal", "Browser", "Close window", "Lock", "Clipboard", "Screenshot",
                "Floating", "Full screen", "File manager", "Alpha extra", "Beta extra", "Gamma extra",
            ])
        ]
        panels = [
            {"id": name, "arg": name, "description": name, "chord": name, "chord_id": name}
            for name in ("Network", "Bluetooth", "Audio", "Power", "Calendar", "Weather", "Display")
        ]
        picked = {command["description"] for command in shortlist(filler + panels, state, {})}
        for name in ("Network", "Bluetooth", "Audio", "Power", "Calendar", "Weather", "Display"):
            self.assertIn(name, picked)


class BarLookupTests(unittest.TestCase):
    def test_the_geometry_cache_does_not_replace_the_lookup(self):
        corner = object.__new__(Corner)
        corner.bar_geometry = []
        corner.bar_geometry_at = 0.0
        corner.pointer = (1170.0, 10.0)
        corner._panel_label = ""
        corner.state_lock = __import__("threading").Lock()
        payload = json.dumps([
            {"id": "omarchy.network", "x": 1164, "y": 0, "width": 27, "height": 26, "visible": True},
            {"id": "omarchy.clock", "x": 585, "y": 0, "width": 111, "height": 26, "visible": True},
        ])
        with patch("corner.daemon.subprocess.check_output", return_value=payload):
            self.assertEqual(corner._layer_event("openlayer", "omarchy-keyboard-panel"), "opened wifi")
            corner.pointer = (600.0, 10.0)
            corner.bar_geometry_at = 0.0
            self.assertEqual(corner._layer_event("openlayer", "omarchy-keyboard-panel"), "opened calendar")
        self.assertEqual(corner._layer_event("closelayer", "omarchy-keyboard-panel"), "closed calendar")
        self.assertTrue(callable(corner._bar_slots))
        self.assertEqual(corner._bar_slots()[0]["id"], "omarchy.network")


class HistoryTests(unittest.TestCase):
    def test_last_apps_are_class_names_newest_first(self):
        apps = remember_app(["Alacritty", "chromium"], "firefox")
        apps = remember_app(apps, "chromium")
        self.assertEqual(apps, ["chromium", "firefox", "Alacritty"])
        self.assertEqual(remember_app(apps, "CHROMIUM"), ["CHROMIUM", "firefox", "Alacritty"])
        self.assertEqual(len(remember_app([f"app{i}" for i in range(10)], "now")), 5)
        self.assertEqual(app_from_activewindow("chromium,Inbox - Gmail, extra"), "chromium")
        self.assertNotIn("Inbox", "".join(remember_app([], app_from_activewindow("chromium,Inbox - Gmail"))))

    def test_history_keeps_the_card_and_drops_an_error(self):
        card = {
            "items": [
                {"arg": "3", "chord": "SUPER + RETURN", "description": "Terminal", "percent": 140},
                {"arg": "nope", "chord": "d", "description": "skip", "percent": 10},
            ],
            "note": "Try",
            "fp": "abc123",
            "at": 50.0,
            "confidence": 0.4,
            "error": "",
            "from_disk": True,
            "spend": "hidden",
            "pool": [{"chord_id": "64:return", "p": 0.4}],
        }
        document = history_document(["firefox", "firefox", "Alacritty"], ["opened wifi", ""], card)
        self.assertEqual(document["apps"], ["firefox", "Alacritty"])
        self.assertEqual(document["just_did"], ["opened wifi"])
        self.assertNotIn("from_disk", json.dumps(document))
        self.assertNotIn("spend", json.dumps(document["result"]))
        self.assertNotIn("pool", json.dumps(document))
        self.assertEqual(document["result"]["items"][0]["percent"], 100)
        self.assertEqual(len(document["result"]["items"]), 1)
        failed = dict(card, error="Jev is busy.")
        self.assertNotIn("result", history_document(["firefox"], ["opened wifi"], failed))
        loaded = load_history(document)
        self.assertTrue(loaded["result"]["from_disk"])
        self.assertEqual(loaded["apps"], ["firefox", "Alacritty"])
        self.assertIsNone(load_history(["nope"])["result"])

    def test_a_saved_card_survives_reboot_without_a_new_call(self):
        saved = {
            "items": [{"arg": "3", "chord": "SUPER + RETURN", "description": "Terminal", "percent": 40}],
            "note": "",
            "error": "",
            "fp": "abc123",
            "at": 0,
            "from_disk": True,
        }
        view = card_payload(saved, 100_000)
        self.assertFalse(view["loading"])
        self.assertEqual(view["items"][0]["chord"], "SUPER + RETURN")
        self.assertTrue(reuse_answer(saved, "abc123", 100_000))
        stale = dict(saved, from_disk=False)
        self.assertFalse(reuse_answer(stale, "abc123", 100_000))
        self.assertEqual(card_payload(stale, 100_000)["items"][0]["chord"], "SUPER + RETURN")
        self.assertFalse(card_payload(stale, 100_000)["loading"])
        empty = {"items": [], "note": "No clear shortcut.", "error": "", "fp": "abc123", "at": 0}
        self.assertEqual(card_payload(empty, 100_000)["items"], [])
        self.assertTrue(card_payload(empty, 100_000)["loading"])
        fresh = dict(stale, at=100_000)
        self.assertTrue(reuse_answer(fresh, "abc123", 100_000))
        self.assertFalse(reuse_answer(dict(saved, error="busy"), "abc123", 100_000))
        self.assertFalse(reuse_answer(saved, "other", 100_000))
        commands = [
            {"arg": "8", "chord": "SUPER + RETURN", "description": "Terminal"},
            {"arg": "2", "chord": "SUPER + W", "description": "Close window"},
        ]
        rebound = rebind_rows(saved["items"], commands)
        self.assertEqual(rebound[0]["arg"], "8")
        self.assertIsNone(rebind_rows(saved["items"], []))
        self.assertEqual(rebind_rows(saved["items"], [{"arg": "1", "chord": "d", "description": "Other"}]), [])
        held = offline_card(saved, commands, "new-screen", 500)
        self.assertEqual(held["items"][0]["arg"], "8")
        self.assertEqual(held["fp"], "new-screen")
        self.assertEqual(held["note"], "")
        self.assertEqual(held["error"], "")
        self.assertEqual(held["charged"], 0)
        self.assertIsNone(offline_card({"items": [], "note": "No clear shortcut.", "fp": "x", "error": ""}, [], "n", 1))
        self.assertIsNone(offline_card({"items": saved["items"], "error": "Jev is busy.", "fp": "x"}, commands, "n", 1))

    def test_a_miss_shows_unused_shortcuts_instead_of_the_last_card(self):
        commands = [
            {"id": "1", "arg": "1", "description": "Terminal", "chord": "SUPER + RETURN", "chord_id": "64:return"},
            {"id": "2", "arg": "2", "description": "Zoom in", "chord": "SUPER + T", "chord_id": "64:t"},
            {"id": "3", "arg": "3", "description": "Emoji picker", "chord": "SUPER + PERIOD", "chord_id": "64:period"},
        ]
        state = {
            "focused": {"app": "chromium", "title": "", "fullscreen": False},
            "also_visible": [],
            "just_did": [],
            "recent_apps": ["chromium"],
        }
        stats = {"64:return": {"uses": 8, "hour": 2, "ago": 5}}
        saved = {
            "items": [{"arg": "9", "chord": "SUPER + RETURN", "description": "Terminal", "percent": 40}],
            "note": "",
            "error": "",
            "fp": "old",
            "at": 0,
        }
        held = held_card(saved, commands, state, stats, "new-screen", 500)
        self.assertEqual([row["description"] for row in held["items"]], ["Zoom in", "Emoji picker"])
        self.assertEqual(held["items"][0]["percent"], "")
        self.assertEqual(held["items"][0]["arg"], "2")
        self.assertEqual(held["fp"], "new-screen")
        self.assertEqual(held["charged"], 0)
        self.assertFalse(held["from_disk"])
        self.assertEqual(rebind_rows(discovery_rows(commands, state, stats), commands)[0]["percent"], "")
        used = {
            "64:return": {"uses": 8, "hour": 1, "ago": 5},
            "64:t": {"uses": 1, "hour": 0, "ago": 60},
            "64:period": {"uses": 1, "hour": 0, "ago": 180},
        }
        fallback = held_card(saved, commands, state, used, "new-screen", 500)
        self.assertEqual(fallback["items"][0]["description"], "Terminal")
        self.assertEqual(fallback["items"][0]["arg"], "1")

    def test_an_app_you_left_still_ranks_its_shortcut(self):
        commands = [
            {"id": "1", "arg": "1", "description": "Alpha", "chord": "SUPER + A", "chord_id": "64:a"},
            {"id": "2", "arg": "2", "description": "Zoom in", "chord": "SUPER + T", "chord_id": "64:t"},
        ]
        plain = {"focused": {"app": "", "title": "", "fullscreen": False}, "also_visible": [], "just_did": [], "recent_apps": []}
        self.assertEqual(shortlist(commands, plain)[0]["description"], "Alpha")
        left = dict(plain, recent_apps=["chromium"])
        self.assertEqual(shortlist(commands, left)[0]["description"], "Zoom in")
        state = screen_state(None, [], ["opened wifi"], ["Alacritty", "chromium"])
        self.assertEqual(state["recent_apps"], ["Alacritty", "chromium"])
        one = build_request(state, commands)
        other = build_request(screen_state(None, [], ["opened wifi"], ["firefox"]), commands)
        self.assertNotEqual(_fingerprint(one), _fingerprint(other))
        self.assertLess(len(json.dumps(one["state"]["recent_apps"])), 80)


class BindTests(unittest.TestCase):
    def test_workspace_loop_and_literal_code(self):
        source = """
        for workspace = 1, 10 do
          local key = "code:" .. tostring(workspace + 9)
          o.bind("SUPER + " .. key, "Switch to workspace " .. workspace, hl.dsp.focus({}))
        end
        o.bind("SUPER + code:34", "Browser back", "x")
        """
        rows = {(row["description"], row["keys"]) for row in parse_bind_keys(source)}
        self.assertIn(("Switch to workspace 1", "SUPER + code:10"), rows)
        self.assertIn(("Switch to workspace 10", "SUPER + code:19"), rows)
        self.assertIn(("Browser back", "SUPER + code:34"), rows)

    def test_a_web_shortcut_keeps_only_the_site(self):
        source = """
        o.bind("SUPER + CTRL + ALT + D", "Calendar", "omarchy-shell shell toggle omarchy.clock")
        o.bind("SUPER + SHIFT + C", "Calendar", { webapp = "https://app.hey.com/calendar/weeks/" })
        o.bind("SUPER + SHIFT + ALT + G", "WhatsApp", { webapp = "https://web.whatsapp.com/", focus = true })
        """
        rows = {row["keys"]: row for row in parse_bind_keys(source)}
        self.assertNotIn("web", rows["SUPER + CTRL + ALT + D"])
        self.assertEqual(rows["SUPER + SHIFT + C"]["web"], "hey.com")
        self.assertEqual(rows["SUPER + SHIFT + ALT + G"]["web"], "whatsapp.com")
        binds = [
            {"mouse": False, "submap": "", "description": "Calendar", "key": "D", "modmask": 76, "arg": "8"},
            {"mouse": False, "submap": "", "description": "Calendar", "key": "C", "modmask": 65, "arg": "9"},
        ]
        commands = commands_from_binds(binds, parse_bind_keys(source), lambda code: "")
        by_chord = {command["chord"]: command for command in commands}
        self.assertNotIn("web", by_chord["SUPER + CTRL + ALT + D"])
        self.assertEqual(by_chord["SUPER + SHIFT + C"]["web"], "hey.com")
        request = build_request(screen_state(None, [], []), commands)
        criteria = request["questions"]["next"]["criteria"]
        self.assertEqual(criteria["9"]["web"], "hey.com")
        self.assertNotIn("web", criteria["8"])
        raw = json.dumps(request)
        self.assertNotIn("https", raw)
        self.assertNotIn("weeks", raw)
        self.assertLess(len(raw), 2000)

    def test_multiline_call_inside_a_loop(self):
        source = """
        for panel = 1, 2 do
          o.bind(
            "SUPER + CTRL + code:" .. tostring(panel + 9),
            "Bar panel " .. panel,
            "toggle " .. panel
          )
        end
        """
        rows = {(row["description"], row["keys"]) for row in parse_bind_keys(source)}
        self.assertIn(("Bar panel 1", "SUPER + CTRL + code:10"), rows)
        self.assertIn(("Bar panel 2", "SUPER + CTRL + code:11"), rows)

    def test_stock_files_cover_empty_hyprland_keys(self):
        if not OMARCHY.exists():
            self.skipTest("omarchy defaults are not installed")
        rows = []
        for path in sorted(OMARCHY.glob("**/*.lua")):
            rows.extend(parse_bind_keys(path.read_text(encoding="utf-8", errors="replace")))
        user = Path.home() / ".config" / "hypr" / "mac-keys.lua"
        if user.exists():
            rows.extend(parse_bind_keys(user.read_text(encoding="utf-8", errors="replace")))
        pairs = {(row["description"], row["keys"]) for row in rows}
        self.assertIn(("Switch to workspace 1", "SUPER + code:10"), pairs)
        self.assertIn(("Switch to workspace 10", "SUPER + code:19"), pairs)
        self.assertIn(("Bar panel 1", "SUPER + CTRL + code:10"), pairs)
        self.assertIn(("Expand window left", "SUPER + code:20"), pairs)
        self.assertIn(("Browser back", "SUPER + code:34"), pairs)
        # This machine moves that resize chord so Cmd+minus can zoom.
        if user.exists():
            self.assertIn(("Expand window left", "SUPER + CTRL + ALT + code:20"), pairs)
            self.assertIn(("Switch to workspace 10", "SUPER + CTRL + code:19"), pairs)

    def test_commands_use_keysym_for_code_binds(self):
        binds = [
            {"mouse": False, "submap": "", "description": "Terminal", "key": "RETURN", "modmask": 64, "arg": "3"},
            {"mouse": False, "submap": "", "description": "Zoom out", "key": "", "modmask": 64, "arg": "9"},
            {"mouse": True, "submap": "", "description": "Move window", "key": "mouse:272", "modmask": 64, "arg": "1"},
            {"mouse": False, "submap": "", "description": "", "key": "Q", "modmask": 64, "arg": "2"},
        ]
        declared = [{"keys": "SUPER + code:20", "description": "Zoom out"}]
        commands = commands_from_binds(binds, declared, lambda code: "minus" if code == 20 else "")
        by_desc = {command["description"]: command for command in commands}
        self.assertEqual(set(by_desc), {"Terminal", "Zoom out"})
        self.assertEqual(by_desc["Terminal"]["chord_id"], chord_id(64, "RETURN"))
        self.assertEqual(by_desc["Zoom out"]["chord_id"], "64:minus")
        self.assertEqual(by_desc["Zoom out"]["chord"], "SUPER + MINUS")

    def test_f23_is_not_offered_as_a_key(self):
        binds = [
            {"mouse": False, "submap": "", "description": "Omarchy menu", "key": "", "modmask": 65, "arg": "4"},
            {"mouse": False, "submap": "", "description": "Omarchy menu", "key": "SPACE", "modmask": 64, "arg": "5"},
            {"mouse": False, "submap": "", "description": "Dictation", "key": "F9", "modmask": 0, "arg": "6"},
        ]
        declared = [{"keys": "SUPER + SHIFT + code:201", "description": "Omarchy menu"}]
        commands = commands_from_binds(binds, declared, lambda code: "F23" if code == 201 else "")
        chords = {command["chord"] for command in commands}
        self.assertNotIn("SUPER + SHIFT + F23", chords)
        self.assertIn("SUPER + SPACE", chords)
        self.assertIn("F9", chords)

    def test_a_locked_hardware_key_stays_off_the_card(self):
        binds = [
            {"mouse": False, "submap": "", "locked": True, "description": "Power menu", "key": "XF86PowerOff", "modmask": 0, "arg": "70"},
            {"mouse": False, "submap": "", "locked": False, "description": "Power", "key": "P", "modmask": 68, "arg": "242"},
        ]
        commands = commands_from_binds(binds, [], lambda code: "")
        self.assertEqual({command["description"] for command in commands}, {"Power"})
        self.assertEqual(commands[0]["chord"], "SUPER + CTRL + P")

    def test_a_chord_is_kept_only_when_the_keyboard_has_that_key(self):
        binds = [
            {"mouse": False, "submap": "", "description": "Omarchy menu", "key": "", "modmask": 65, "arg": "4"},
            {"mouse": False, "submap": "", "description": "Terminal", "key": "RETURN", "modmask": 64, "arg": "3"},
            {"mouse": False, "submap": "", "description": "Zoom out", "key": "", "modmask": 64, "arg": "9"},
        ]
        declared = [
            {"keys": "SUPER + SHIFT + code:201", "description": "Omarchy menu"},
            {"keys": "SUPER + code:20", "description": "Zoom out"},
        ]

        def names(code: int) -> str:
            return {201: "F23", 20: "minus"}.get(code, "")

        commands = commands_from_binds(binds, declared, names, {"return", "minus"})
        self.assertEqual(
            {command["description"] for command in commands},
            {"Terminal", "Zoom out"},
        )

    def test_corner_debug_is_one_learned_chord(self):
        binds = [
            {"mouse": False, "submap": "", "description": "Corner debug", "key": "d", "modmask": 0, "arg": "11"},
            {"mouse": False, "submap": "", "description": "Corner debug", "key": "D", "modmask": 0, "arg": "12"},
            {"mouse": False, "submap": "", "description": "Corner debug", "key": "d", "modmask": 1, "arg": "13"},
            {"mouse": False, "submap": "", "description": "Terminal", "key": "RETURN", "modmask": 64, "arg": "3"},
        ]
        commands = commands_from_binds(binds, [], lambda code: "")
        debug = [command for command in commands if command["description"] == "Corner debug"]
        self.assertEqual(len(debug), 1)
        self.assertEqual(debug[0]["chord_id"], CORNER_DEBUG_CHORD)
        state = {"focused": {"app": "", "title": "", "fullscreen": False}, "also_visible": [], "just_did": []}
        now = 1_000_000.0
        stats = chord_stats([{"t": now, "chord": CORNER_DEBUG_CHORD} for _ in range(6)], now)
        picked = shortlist(commands, state)
        self.assertIn(CORNER_DEBUG_CHORD, {command["chord_id"] for command in picked})
        fact = build_request(state, picked, stats)["questions"]["next"]["criteria"][debug[0]["id"]]
        self.assertEqual(fact["uses"], 6)
        self.assertEqual(fact["hour"], 6)
        self.assertEqual(fact["ago"], 0)
        self.assertEqual(fact["does"], "Corner debug")

    def test_corner_debug_log_is_counted_once(self):
        now = 1_000_000.0
        seeded = seed_corner_debug([], 8, now - 20, now)
        self.assertEqual(len(seeded), 8)
        again = seed_corner_debug(seeded, 8, now - 20, now)
        self.assertEqual(len(again), 8)
        old = seed_corner_debug([], 8, now - 90000, now)
        self.assertEqual(len(old), 8)
        self.assertEqual(len(seed_corner_debug(old, 8, now - 90000, now)), 8)


class RankTests(unittest.TestCase):
    def test_a_used_chord_reaches_jev_with_its_counts(self):
        commands = [
            {"id": "1", "arg": "1", "description": "Terminal", "chord": "SUPER + RETURN", "chord_id": "64:return"},
            {"id": "2", "arg": "2", "description": "Close window", "chord": "SUPER + W", "chord_id": "64:w"},
        ]
        state = {"focused": {"app": "chromium", "title": "mail", "fullscreen": False}, "also_visible": [], "just_did": []}
        picked = shortlist(commands, state)
        self.assertIn("64:return", {command["chord_id"] for command in picked})
        stats = {"64:return": {"uses": 9, "hour": 2, "ago": 15}}
        request = build_request(state, picked, stats)
        self.assertEqual(set(request["questions"]), {"next"})
        fact = request["questions"]["next"]["criteria"]["1"]
        self.assertEqual(fact["uses"], 9)
        self.assertEqual(fact["hour"], 2)
        self.assertEqual(fact["ago"], 15)
        self.assertEqual(request["questions"]["next"]["criteria"]["2"]["uses"], 0)
        self.assertNotIn("hour", request["questions"]["next"]["criteria"]["2"])
        again = build_request(state, picked, stats)
        self.assertEqual(_fingerprint(request), _fingerprint(again))
        bumped = build_request(state, picked, {"64:return": {"uses": 10, "hour": 2, "ago": 15}})
        self.assertNotEqual(_fingerprint(request), _fingerprint(bumped))

    def test_top_five_follow_probability(self):
        commands = [
            {"id": str(i), "arg": str(i), "description": f"Command {i}", "chord": str(i), "chord_id": str(i)}
            for i in range(8)
        ]
        probabilities = {str(i): (0.5 if i == 3 else 0.01 * i) for i in range(8)}
        ranked = rank(commands, probabilities, 0.9)
        self.assertEqual([row["id"] for row in ranked], ["3", "7", "6", "5", "4"])
        rows = suggestion_rows(ranked)
        self.assertEqual(rows[0]["percent"], 50)
        self.assertEqual(len(rows), 5)

    def test_low_confidence_shows_nothing(self):
        commands = [
            {"id": "3", "arg": "3", "description": "Terminal", "chord": "SUPER + RETURN", "chord_id": "64:return"},
        ]
        probabilities = {"3": 0.9}
        self.assertEqual(rank(commands, probabilities, SHOW_CONFIDENCE - 0.01), [])
        self.assertEqual(rank(commands, probabilities, None), [])
        shown = rank(commands, probabilities, SHOW_CONFIDENCE)
        self.assertEqual([row["id"] for row in shown], ["3"])

    def test_a_few_leading_shortcuts_are_not_an_empty_card(self):
        # Twelve options, top probability 0.22. That is confidence about 0.15.
        # The old 0.2 floor treated it as no pick.
        commands = [
            {"id": str(i), "arg": str(i), "description": f"Command {i}", "chord": str(i), "chord_id": str(i)}
            for i in range(12)
        ]
        probabilities = {str(i): 0.02 for i in range(12)}
        probabilities["3"] = 0.22
        probabilities["4"] = 0.16
        confidence = (12 * 0.22 - 1) / 11
        self.assertLess(confidence, 0.2)
        self.assertGreaterEqual(confidence, SHOW_CONFIDENCE)
        ranked = rank(commands, probabilities, confidence)
        self.assertEqual([row["id"] for row in ranked[:2]], ["3", "4"])

    def test_known_shortcuts_leave_room_for_new_ones(self):
        names = [
            "Terminal", "Browser", "Close window", "Lock", "Clipboard", "Screenshot",
            "Floating", "Full screen", "Omarchy menu", "File manager", "Alpha extra", "Beta extra",
        ]
        commands = [
            {"id": str(i), "arg": str(i), "description": name, "chord": name, "chord_id": f"used:{i}"}
            for i, name in enumerate(names)
        ]
        commands.append({
            "id": "20", "arg": "20", "description": "Emoji picker",
            "chord": "SUPER + PERIOD", "chord_id": "new:emoji",
        })
        commands.append({
            "id": "21", "arg": "21", "description": "Zoom in",
            "chord": "SUPER + EQUAL", "chord_id": "new:zoom",
        })
        state = {
            "focused": {"app": "", "title": "", "fullscreen": False},
            "also_visible": [], "just_did": [], "recent_apps": [],
        }
        stats = {f"used:{i}": {"uses": 8, "ago": 0, "hour": 1} for i in range(len(names))}
        picked = shortlist(commands, state, stats)
        ident = [command["chord_id"] for command in picked]
        self.assertEqual(set(ident[-2:]), {"new:zoom", "new:emoji"})
        self.assertLessEqual(len(picked), 12)
        browsing = dict(state, focused={"app": "chromium", "title": "", "fullscreen": False})
        on_screen = [command["chord_id"] for command in shortlist(commands, browsing, stats)]
        self.assertIn("new:zoom", on_screen)

    def test_a_forgotten_shortcut_is_still_offered(self):
        names = [
            "Terminal", "Browser", "Close window", "Lock", "Clipboard", "Screenshot",
            "Floating", "Full screen", "Omarchy menu", "File manager", "Alpha extra", "Beta extra",
        ]
        commands = [
            {"id": str(i), "arg": str(i), "description": name, "chord": name, "chord_id": f"c:{i}"}
            for i, name in enumerate(names)
        ]
        commands.append({
            "id": "30", "arg": "30", "description": "Emoji picker",
            "chord": "SUPER + PERIOD", "chord_id": "old:emoji",
        })
        now = 2_000_000_000.0
        events = [{"t": now - 30, "chord": f"c:{i}"} for i in range(len(names))]
        events.append({"t": now - 40 * 86400, "chord": "old:emoji"})
        stats = chord_stats(events, now)
        self.assertEqual(stats["old:emoji"], {"uses": 1, "ago": 43200})
        state = {
            "focused": {"app": "", "title": "", "fullscreen": False},
            "also_visible": [], "just_did": [], "recent_apps": [],
        }
        picked = shortlist(commands, state, stats)
        self.assertIn("old:emoji", {command["chord_id"] for command in picked})
        fact = build_request(state, picked, stats)["questions"]["next"]["criteria"]["30"]
        self.assertEqual(fact["ago"], 43200)
        self.assertIn("forgotten", build_request(state, picked, stats)["questions"]["next"]["instructions"])

    def test_a_zero_from_jev_stays_off_the_card(self):
        commands = [
            {"id": "1", "arg": "1", "description": "Corner debug", "chord": "d", "chord_id": "corner-debug"},
            {"id": "2", "arg": "2", "description": "Terminal", "chord": "SUPER + RETURN", "chord_id": "64:return"},
        ]
        ranked = rank(commands, {"1": 0.0, "2": 0.8}, 0.9)
        self.assertEqual([row["id"] for row in ranked], ["2"])

    def test_request_is_one_choice_over_the_shortlist(self):
        commands = [
            {"id": "3", "arg": "3", "description": "Terminal", "chord": "SUPER + RETURN", "chord_id": "64:return"},
        ]
        state = screen_state(None, [], [])
        request = build_request(state, commands)
        question = request["questions"]["next"]
        self.assertEqual(request["model"], "jev-latest")
        self.assertEqual(set(request["questions"]), {"next"})
        self.assertEqual(question["type"], "choice")
        self.assertEqual(question["criteria"]["3"], {"chord": "SUPER + RETURN", "does": "Terminal", "uses": 0})
        self.assertIn("spread probability evenly", question["instructions"])
        self.assertIn("recent_apps", question["instructions"])
        self.assertEqual(request["state"]["recent_apps"], [])
        raw = json.dumps(request)
        self.assertLess(len(raw), 2000)
        self.assertNotIn("shown", raw)


class UnusedListTests(unittest.TestCase):
    def _commands(self):
        weights = (0.5, 0.2, 0.1, 0.05, 0.04, 0.03, 0.02)
        return [
            {
                "id": str(i),
                "arg": str(i),
                "description": f"Command {i}",
                "chord": f"C{i}",
                "chord_id": f"id:{i}",
            }
            for i in range(len(weights))
        ]

    def _aside(self, ident: str, at: float = 100) -> dict:
        return {ident: {"shows": UNUSED_SHOWS, "penalty": 1.0, "at": at}}

    def test_five_ignores_set_it_aside_and_a_day_brings_it_back(self):
        unused = {}
        for step in range(UNUSED_SHOWS):
            unused = note_unused(unused, ["a"], (), 100)
            self.assertEqual(unused["a"]["shows"], step + 1)
        self.assertEqual(unused["a"]["penalty"], 1.0)
        self.assertEqual(unused_penalty(unused, "a", 100), 1.0)
        self.assertAlmostEqual(unused_penalty(unused, "a", 100 + UNUSED_FORGET_SECONDS / 2), 0.5)
        self.assertEqual(unused_penalty(unused, "a", 100 + UNUSED_FORGET_SECONDS), 0.0)
        faded = note_unused(unused, ["b"], (), 100 + UNUSED_FORGET_SECONDS)
        self.assertNotIn("a", faded)
        again = note_unused(unused, ["a"], (), 100 + UNUSED_FORGET_SECONDS / 2)
        self.assertEqual(again["a"]["shows"], UNUSED_SHOWS + 1)
        self.assertAlmostEqual(again["a"]["penalty"], 0.7)
        self.assertEqual(note_unused(again, ["a"], ["a"], 100), {})

    def test_presses_this_hour_set_it_aside_even_after_the_ignore_count_clears(self):
        commands = [
            {"id": "1", "arg": "1", "description": "Universal copy", "chord": "SUPER + C", "chord_id": "64:c", "p": 0.5, "pool": "app"},
            {"id": "2", "arg": "2", "description": "Omarchy menu", "chord": "SUPER + SPACE", "chord_id": "64:space", "p": 0.2, "pool": "app"},
        ]
        heavy = {"64:c": {"uses": 12, "hour": 12, "ago": 0}}
        shown = [row["description"] for row in offer_rows(commands, {}, stats=heavy)]
        self.assertEqual(shown, ["Omarchy menu"])
        light = {"64:c": {"uses": 2, "hour": 2, "ago": 0}}
        eased = offer_rows(commands, {}, stats=light)
        self.assertEqual(eased[0]["description"], "Universal copy")
        self.assertEqual(round(eased[0]["p"] * 100), 30)

    def test_one_ignore_takes_a_fifth_and_a_lower_score_can_lead(self):
        commands = [
            {"id": "1", "arg": "1", "description": "Often", "chord": "A", "chord_id": "a", "p": 0.5},
            {"id": "2", "arg": "2", "description": "Other", "chord": "B", "chord_id": "b", "p": 0.42},
        ]
        once = note_unused({}, ["a"], (), 10)
        self.assertEqual(once["a"]["penalty"], 0.2)
        leading = offer_rows(commands, once, now=10)
        self.assertEqual(leading[0]["description"], "Other")
        self.assertEqual(leading[1]["percent"] if "percent" in leading[1] else round(leading[1]["p"] * 100), 40)

    def test_a_full_penalty_yields_and_a_use_clears_it(self):
        commands = self._commands()
        weights = (0.5, 0.2, 0.1, 0.05, 0.04, 0.03, 0.02)
        probabilities = {command["id"]: weight for command, weight in zip(commands, weights)}
        aside = {f"id:{i}": {"shows": UNUSED_SHOWS, "penalty": 1.0, "at": 10} for i in range(5)}
        rows, ranked = suggest_rows(commands, probabilities, 0.9, aside, 10)
        self.assertEqual([row["arg"] for row in rows], ["5", "6"])
        self.assertEqual(len(ranked), len(commands))
        everyone = {f"id:{i}": {"shows": UNUSED_SHOWS, "penalty": 1.0, "at": 10} for i in range(7)}
        parked, _ = suggest_rows(commands, probabilities, 0.9, everyone, 10)
        self.assertEqual([row["arg"] for row in parked], ["0", "1", "2", "3", "4"])
        self.assertEqual(note_unused(aside, ["id:0"], ["id:0"], 10), {key: value for key, value in aside.items() if key != "id:0"})

    def test_the_unused_list_stays_out_of_the_jev_request(self):
        commands = self._commands()
        state = screen_state(None, [], [])
        request = build_request(state, commands, {"id:0": {"uses": 4, "hour": 1, "ago": 5}})
        raw = json.dumps(request)
        self.assertNotIn("shown", raw)
        self.assertNotIn("unused.json", raw)
        self.assertNotIn("streak", request["questions"]["next"]["instructions"])
        self.assertLess(len(raw), 2000)
        self.assertEqual(_fingerprint(request), _fingerprint(build_request(state, commands, {"id:0": {"uses": 4, "hour": 1, "ago": 5}})))

    def test_discovery_skips_a_fully_aside_shortcut_until_everyone_is_aside(self):
        commands = [
            {"arg": "1", "chord": "A", "description": "Alpha", "chord_id": "a"},
            {"arg": "2", "chord": "B", "description": "Beta", "chord_id": "b"},
        ]
        aside = self._aside("a")
        self.assertEqual([item["chord_id"] for item in available_commands(commands, aside, 100)], ["b"])
        both = {**self._aside("a"), **self._aside("b")}
        self.assertEqual([item["chord_id"] for item in available_commands(commands, both, 100)], ["a", "b"])
        state = {"focused": {"app": "", "title": "", "fullscreen": False}, "also_visible": [], "just_did": []}
        items = [
            {"arg": "1", "chord": "A", "description": "Alpha", "percent": 40},
            {"arg": "2", "chord": "B", "description": "Beta", "percent": 20},
        ]
        rotated = rotate_saved(items, commands, aside, state, {}, now=100)
        self.assertEqual([row["chord"] for row in rotated], ["B"])
        kept = rotate_saved(items, commands, both, state, {}, now=100)
        self.assertEqual([row["chord"] for row in kept], ["A", "B"])

    def test_unused_file_counts_one_visit_and_a_use_clears_it(self):
        directory = Path("/tmp/shortcuts-ai-unused-test")
        directory.mkdir(exist_ok=True)
        path = directory / "unused.json"
        if path.exists():
            path.unlink()
        corner = object.__new__(Corner)
        corner.state_lock = threading.Lock()
        corner.shows_path = directory / "shown.json"
        corner.unused_path = path
        corner.unused = {}
        corner._display_ids = []
        corner._used_now = set()
        corner.phase = "approach"
        corner.session = 4
        corner._commands = lambda: [{"arg": "9", "chord": "SUPER + C", "description": "Calendar", "chord_id": "65:c"}]
        corner._remember_display([{"chord": "SUPER + C", "description": "Calendar"}])
        self.assertEqual(corner._display_ids, [])
        corner.phase = "shown"
        corner._remember_display([{"chord": "SUPER + C", "description": "Calendar"}])
        self.assertEqual(corner._display_ids, ["65:c"])
        path.write_text(
            '{"65:c": {"shows": 4, "penalty": 0.8, "at": 99999999999}, "": {"shows": 1, "penalty": 0.2, "at": 99999999999}, "bad": "x"}\n',
            encoding="utf-8",
        )
        corner._load_unused()
        self.assertEqual(corner.unused["65:c"]["shows"], 4)
        self.assertNotIn("", corner.unused)
        self.assertNotIn("bad", corner.unused)
        emitted = []
        corner.emit = lambda payload: emitted.append(payload)
        corner._hide_now()
        self.assertEqual(corner.phase, "away")
        self.assertEqual(corner.session, 5)
        self.assertEqual(corner.unused["65:c"]["shows"], 5)
        self.assertEqual(corner.unused["65:c"]["penalty"], 1.0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["65:c"]["shows"], 5)
        self.assertEqual(emitted, [{"op": "hide", "session": 4}])
        self.assertEqual(corner._display_ids, [])
        held = corner.unused["65:c"]["shows"]
        corner._hide_now()
        self.assertEqual(corner.unused["65:c"]["shows"], held)
        self.assertEqual(len(emitted), 1)
        corner.phase = "shown"
        corner._display_ids = ["65:c"]
        corner._mark_used("65:c")
        self.assertEqual(corner.unused, {})
        self.assertEqual(corner._used_now, {"65:c"})
        corner._hide_now()
        self.assertNotIn("65:c", corner.unused)
        path.unlink(missing_ok=True)
        directory.rmdir()


class HookTests(unittest.TestCase):
    def test_plain_typing_is_not_recorded(self):
        source = hook_source("/home/ignotas/.local/state/omarchy/shortcuts-ai/chords.log")
        self.assertIn(f"mods < 4 and code < {FUNCTION_KEYCODE}", source)
        self.assertIn("how ~= 1", source)
        self.assertIn("ignotas_shortcuts_ai_hook", source)
        self.assertNotIn("apikey_", source)
        # Lua string.format must see %% and \\n, so the shell printf keeps them.
        self.assertIn("printf '%%d %%d %%d\\\\n'", source)

    def test_refuses_a_path_that_would_break_the_lua_string(self):
        with self.assertRaises(ValueError):
            hook_source('/tmp/bad"path')
        with self.assertRaises(ValueError):
            hook_source("/tmp/bad'path")

    def test_d_is_swallowed_only_while_the_corner_is_armed(self):
        source = dkey_source("/home/ignotas/.local/state/omarchy/shortcuts-ai/dkey.log", 40)
        self.assertIn('pcall(hl.bind, spec, bite', source)
        self.assertIn('"d"', source)
        self.assertIn('"D"', source)
        self.assertIn('"SHIFT + d"', source)
        self.assertIn('"SHIFT + D"', source)
        self.assertIn("os.clock()", source)
        self.assertNotIn("CTRL", source)
        self.assertNotIn("ALT", source)
        self.assertNotIn("SUPER", source)
        self.assertIn("ignotas_shortcuts_ai_set_arm", source)
        self.assertIn('return "bound"', source)
        self.assertIn('return "unbound"', source)
        self.assertIn('return "same"', source)
        self.assertIn("bind:unbind()", source)
        self.assertNotIn("hl.unbind", source)
        self.assertNotIn("non_consuming", source)
        self.assertNotIn("input.keyboard.key", source)
        self.assertNotIn("chords.log", source)
        self.assertIn("printf '1\\\\n'", source)
        with self.assertRaises(ValueError):
            dkey_source("/tmp/bad'path", 40)
        with self.assertRaises(ValueError):
            dkey_source("/tmp/ok", 0)
        with self.assertRaises(ValueError):
            dkey_source("/tmp/ok", 256)


class ZoneTests(unittest.TestCase):
    def test_scale_two_corner_is_the_logical_bottom_right(self):
        monitor = {"name": "eDP-1", "x": 0, "y": 0, "width": 2560, "height": 1600, "scale": 2, "transform": 0}
        self.assertEqual(_layout_box(monitor), (0.0, 0.0, 1280.0, 800.0))
        self.assertEqual(_zone(1275, 795, [monitor], "away", "")[0], "show")
        self.assertEqual(_zone(1210, 730, [monitor], "away", "")[0], "approach")
        self.assertEqual(_zone(400, 400, [monitor], "away", "")[0], "away")
        self.assertEqual(_zone(900, 600, [monitor], "shown", "eDP-1")[0], "keep")
        self.assertEqual(_zone(400, 400, [monitor], "shown", "eDP-1")[0], "away")
        # The debug pane sits to the left. 1180px from the right edge is still on it.
        self.assertEqual(_zone(100, 700, [monitor], "shown", "eDP-1")[0], "away")
        self.assertEqual(_zone(100, 700, [monitor], "shown", "eDP-1", DEBUG_KEEP_W)[0], "keep")
        self.assertTrue(_arms_d("show"))
        self.assertTrue(_arms_d("keep"))
        self.assertFalse(_arms_d("approach"))
        self.assertFalse(_arms_d("away"))


class SpendTests(unittest.TestCase):
    def test_a_million_input_tokens_cost_four_point_two_cents(self):
        self.assertAlmostEqual(jev_cost(1_000_000), 0.042)
        self.assertEqual(jev_cost(0), 0)

    def test_output_tokens_are_not_billed(self):
        total = add_jev_spend(None, 500, 80)
        total = add_jev_spend(total, 500, 80)
        self.assertEqual(total["calls"], 2)
        self.assertEqual(total["input_tokens"], 1000)
        self.assertEqual(total["output_tokens"], 160)
        self.assertAlmostEqual(total["usd"], jev_cost(1000))

    def test_missing_usage_is_zero(self):
        self.assertEqual(jev_usage({}), (0, 0))
        self.assertEqual(jev_usage({"usage": {"input_tokens": 312, "output_tokens": 4}}), (312, 4))

    def test_the_card_line_is_cents_until_a_cent(self):
        self.assertEqual(format_usd(0.0000168), "0.0017¢")
        total = add_jev_spend(None, 400, 0)
        self.assertEqual(spend_line(total, 400), f"Jev {format_usd(jev_cost(400))} · {format_usd(jev_cost(400))} over 1")
        self.assertEqual(spend_line(total, 0), f"Jev {format_usd(jev_cost(400))} over 1")
        self.assertEqual(spend_line(total, 0, 0.42), f"Jev {format_usd(jev_cost(400))} over 1 · 0.42")
        self.assertEqual(spend_line(None), "")

    def test_debug_hides_the_cost_until_it_is_on(self):
        self.assertEqual(debug_line(False, "Jev 0.0025¢ over 1"), "")
        self.assertEqual(debug_line(True, ""), "debug")
        self.assertEqual(debug_line(True, "Jev 0.0025¢ over 1"), "debug · Jev 0.0025¢ over 1")

    def test_debug_pane_carries_the_last_input_only_while_it_is_on(self):
        corner = object.__new__(Corner)
        corner.state_lock = threading.Lock()
        corner.debug = True
        corner.last_input = '{\n  "model": "jev-latest"\n}'
        corner.mac_keys = True
        sent = []
        corner.emit = lambda payload: sent.append(payload)
        corner._emit_card({"op": "update"})
        self.assertIn("jev-latest", sent[0]["input"])
        self.assertNotIn("apikey_", sent[0]["input"])
        self.assertTrue(sent[0]["mac"])
        corner.debug = False
        corner._emit_card({"op": "update"})
        self.assertEqual(sent[1]["input"], "")
        self.assertTrue(sent[1]["mac"])

    def test_debug_input_puts_each_criterion_on_one_line(self):
        commands = [
            {"id": "3", "arg": "3", "description": "Terminal", "chord": "SUPER + RETURN", "chord_id": "64:return"},
            {
                "id": "9", "arg": "9", "description": "Calendar", "chord": "SUPER + SHIFT + C",
                "chord_id": "65:c", "web": "hey.com",
            },
        ]
        state = screen_state(None, [], ["opened wifi"])
        request = build_request(state, commands, {"65:c": {"uses": 1, "ago": 1440}})
        before = json.dumps(request, sort_keys=True)
        text = format_debug_input(request)
        self.assertEqual(json.dumps(request, sort_keys=True), before)
        self.assertEqual(json.loads(text), request)
        self.assertEqual(_fingerprint(request), _fingerprint(json.loads(text)))
        criteria = request["questions"]["next"]["criteria"]
        lines = text.splitlines()
        start = next(index for index, line in enumerate(lines) if '"criteria"' in line)
        indent = len(lines[start]) - len(lines[start].lstrip(" "))
        end = next(index for index in range(start + 1, len(lines)) if lines[index] == (" " * indent) + "}")
        block = lines[start:end + 1]
        self.assertEqual(len(block), 2 + len(criteria))
        for fact in criteria.values():
            self.assertIn(json.dumps(fact, ensure_ascii=False), text)
        self.assertIn('"focused": {\n', text)
        self.assertNotIn("apikey_", text)

    def test_the_pane_d_flips_debug_without_a_press(self):
        corner = object.__new__(Corner)
        corner.state_lock = threading.Lock()
        corner.debug = True
        corner.phase = "shown"
        corner.session = 4
        corner.result = {"items": [], "note": "Try", "error": "", "spend": "Jev 0.0025¢ over 1"}
        corner.last_input = '{"model": "jev-latest"}'
        corner.mac_keys = True
        corner.usage = [{"t": 1, "chord": "64:return"}]
        sent = []
        corner.emit = lambda payload: sent.append(payload)
        with tempfile.TemporaryDirectory() as tmp:
            corner.debug_path = Path(tmp) / "debug"
            corner.toggle_from_card()
            self.assertEqual(corner.debug_path.read_text(encoding="utf-8"), "0\n")
        self.assertFalse(corner.debug)
        self.assertEqual(corner.usage, [{"t": 1, "chord": "64:return"}])
        self.assertEqual(sent[0]["op"], "update")
        self.assertEqual(sent[0]["spend"], "")
        self.assertEqual(sent[0]["input"], "")
        self.assertEqual(sent[0]["session"], 4)


class KeyboardTests(unittest.TestCase):
    def test_apple_keyboard_bitmap_has_letters_and_not_f23(self):
        # Published by the kernel for this machine's Apple SPI keyboard.
        mask = "10000 0 0 0 6300001000 3800000000 710effd063c0001f ff7ffffffffffffe"
        bits = key_bits(mask)
        self.assertIn(1, bits)    # Esc
        self.assertIn(12, bits)   # minus
        self.assertIn(28, bits)   # Enter
        self.assertIn(57, bits)   # Space
        self.assertIn(59, bits)   # F1
        self.assertNotIn(193, bits)  # F23, which XKB invents for keycode 201

    def test_virtual_keyboards_are_not_the_source_of_keys(self):
        text = """
N: Name="hl-virtual-keyboard"
H: Handlers=kbd event9
B: KEY=ffffffffffffffff

N: Name="Video Bus"
H: Handlers=kbd event6
B: KEY=ff

N: Name="Power Button"
H: Handlers=kbd event1
B: KEY=4

N: Name="Apple SPI Keyboard"
H: Handlers=sysrq kbd leds event4
B: KEY=2
"""
        # Apple contributes bit 1. The power button contributes bit 2.
        # The virtual keyboard and the video bus are not keys the user presses.
        self.assertEqual(evdev_codes(text), {1, 2})

    def test_a_mac_names_command_and_option(self):
        self.assertTrue(apple_hardware("Apple Inc.", "MacBookPro14,1", ""))
        self.assertTrue(apple_hardware("", "", 'N: Name="Apple SPI Keyboard"\nH: Handlers=kbd'))
        self.assertFalse(apple_hardware("Dell Inc.", "XPS 13", 'N: Name="AT Translated Set 2 keyboard"\nH: Handlers=kbd'))


class XkbTests(unittest.TestCase):
    def test_gb_layout_names_the_number_row(self):
        try:
            from corner.xkb import Keysyms
            keys = Keysyms("gb")
        except (OSError, RuntimeError) as exc:
            self.skipTest(str(exc))
        try:
            self.assertEqual(keys.name(10), "1")
            self.assertEqual(keys.name(19), "0")
            self.assertEqual(keys.name(20), "minus")
            self.assertEqual(keys.name(21), "equal")
            self.assertEqual(keys.name(36), "Return")
        finally:
            keys.close()


class FullRankTests(unittest.TestCase):
    def _command(self, ident: str, description: str) -> dict:
        return {
            "id": ident,
            "arg": ident,
            "description": description,
            "chord": f"SUPER + {ident}",
            "chord_id": f"64:{ident}",
        }

    def test_a_panel_joins_the_same_list_as_an_app(self):
        self.assertEqual(used_layer_name("omarchy-keyboard-panel", "omarchy.network"), "wifi")
        self.assertEqual(used_layer_name("omarchy-menu"), "menu")
        self.assertEqual(used_layer_name("omarchy-bar"), "")
        self.assertEqual(used_layer_name("omarchy-keyboard-panel", ""), "")
        apps = remember_app(["Alacritty"], used_layer_name("omarchy-keyboard-panel", "omarchy.network"))
        self.assertEqual(apps, ["wifi", "Alacritty"])
        self.assertTrue(same_used(apps, ["Alacritty", "wifi"]))
        self.assertFalse(same_used(apps, ["Alacritty"]))

    def test_a_saved_rank_is_reused_until_the_names_change(self):
        rank = {
            "apps": ["Alacritty", "wifi"],
            "probabilities": {"3": 0.4, "9": 0.02, "bad": 0.5, "4": 2},
            "confidence": 0.2,
            "explore": {"1": 0.3, "0": 0, "nope": 0.4},
            "explore_confidence": 0.1,
            "offered": ["64:1"],
            "at": 10,
        }
        self.assertTrue(needs_full_rank({**rank, "explore": None}, ["wifi", "Alacritty"]))
        held = {key: value for key, value in rank.items() if key != "explore"}
        self.assertTrue(needs_full_rank(held, ["wifi", "Alacritty"]))
        pending = {key: value for key, value in rank.items() if key != "offered"}
        self.assertTrue(needs_full_rank(pending, ["wifi", "Alacritty"]))
        self.assertFalse(needs_full_rank(rank, ["wifi", "Alacritty"]))
        self.assertTrue(needs_full_rank(rank, ["wifi", "firefox"]))
        self.assertTrue(needs_full_rank(None, ["wifi"]))
        document = history_document(["Alacritty", "wifi"], [], None, rank)
        loaded = load_history(document)
        self.assertEqual(loaded["rank"]["probabilities"], {"3": 0.4, "9": 0.02})
        self.assertEqual(loaded["rank"]["explore"], {"1": 0.3})
        self.assertEqual(loaded["rank"]["explore_confidence"], 0.1)
        self.assertTrue(same_used(loaded["rank"]["apps"], ["wifi", "Alacritty"]))
        self.assertIsNone(load_history({"rank": {"apps": ["wifi"], "probabilities": {}}})["rank"])
        empty = load_history({"rank": {"apps": ["wifi"], "probabilities": {}, "explore": {}, "offered": []}})["rank"]
        self.assertEqual(empty["explore"], {})
        self.assertEqual(empty["offered"], [])
        self.assertFalse(needs_full_rank(empty, ["wifi"]))

    def test_two_presses_of_a_suggestion_rank_again(self):
        rank = {
            "apps": ["Alacritty", "wifi"],
            "probabilities": {"1": 0.5},
            "explore": {"3": 0.2},
            "offered": ["64:1", "64:2"],
            "at": 100,
        }
        presses = [{"t": 110, "chord": "64:1"}, {"t": 120, "chord": "64:1"}]
        self.assertFalse(needs_full_rank(rank, ["wifi", "Alacritty"], presses[:1], 130))
        self.assertTrue(needs_full_rank(rank, ["wifi", "Alacritty"], presses, 130))
        self.assertFalse(needs_full_rank(rank, ["wifi", "Alacritty"], [
            {"t": 90, "chord": "64:1"},
            {"t": 95, "chord": "64:1"},
        ], 130))
        self.assertFalse(needs_full_rank(rank, ["wifi", "Alacritty"], [
            {"t": 110, "chord": "64:1"},
            {"t": 120, "chord": "64:9"},
        ], 130))
        self.assertFalse(needs_full_rank(rank, ["wifi", "Alacritty"], [
            {"t": 110, "chord": "64:1"},
            {"t": 120, "chord": "64:2"},
        ], 130))
        document = history_document(["Alacritty", "wifi"], [], None, rank)
        loaded = load_history(document)
        self.assertEqual(loaded["rank"]["offered"], ["64:1", "64:2"])

    def test_the_card_is_a_cut_of_the_saved_ranking(self):
        commands = [
            self._command("1", "Terminal"),
            self._command("2", "Network"),
            self._command("3", "Lock system"),
            self._command("4", "Browser"),
        ]
        probabilities = {"1": 0.02, "2": 0.5, "3": 0.4, "4": 0.9}
        self.assertTrue(matches_used(commands[0], ["Alacritty"]))
        self.assertTrue(matches_used(commands[1], ["wifi"]))
        rows, pool = slice_rows(commands, probabilities, ["Alacritty"])
        shown = [row["description"] for row in rows]
        self.assertEqual(shown[0], "Terminal")
        self.assertEqual(pool[0]["p"], 1)
        self.assertIn("Lock system", shown)
        wide = [row["description"] for row in slice_rows(commands, probabilities, ["Alacritty", "wifi", "chromium"])[0]]
        self.assertEqual(wide[:2], ["Browser", "Network"])
        menus = [
            self._command("5", "Omarchy menu"),
            self._command("6", "Capture menu"),
            self._command("7", "System menu"),
        ]
        terminal = [self._command("8", "Terminal"), self._command("9", "Tmux keybindings")]
        shares = {"5": 0.02, "6": 0.02, "7": 0.02, "8": 0.02, "9": 0.02}
        ordered = [row["description"] for row in slice_rows(terminal + menus, shares, ["Alacritty", "menu"])[0]]
        self.assertEqual(ordered[:2], ["Terminal", "Tmux keybindings"])

    def test_the_latest_mouse_effect_leads_its_shortcut(self):
        commands = [
            self._command("1", "Terminal"),
            self._command("2", "Network"),
            self._command("3", "Browser"),
            self._command("4", "Close window"),
            self._command("5", "Quit (close window)"),
            self._command("6", "Switch to workspace 2"),
            self._command("7", "Move window to workspace 2"),
            self._command("8", "Switch to workspace 10"),
            self._command("9", "Switch to workspace 1"),
        ]
        probabilities = {"1": 0.5, "3": 0.9, "2": 0.05}
        rows, pool = slice_rows(commands, probabilities, ["Alacritty"], actions=["opened wifi"])
        self.assertEqual(rows[0]["description"], "Network")
        self.assertEqual(rows[0]["percent"], 5)
        self.assertTrue(pool[0].get("led"))
        self.assertEqual(rows[1]["description"], "Terminal")

        switched = slice_rows(
            commands, probabilities, ["Alacritty"],
            actions=["workspace 2", "focused chromium"],
        )[0]
        self.assertEqual(switched[0]["description"], "Switch to workspace 2")
        self.assertEqual(switched[0]["percent"], "")

        numbered = slice_rows(
            commands, probabilities, ["Alacritty"],
            actions=["workspace 1"],
        )[0]
        self.assertEqual(numbered[0]["description"], "Switch to workspace 1")

        closed = slice_rows(
            commands, probabilities, ["Alacritty"],
            actions=["closed a window"],
        )[0]
        self.assertEqual(closed[0]["description"], "Close window")

        untouched = slice_rows(
            commands, probabilities, ["Alacritty"],
            actions=["focused chromium"],
        )[0]
        self.assertEqual(untouched[0]["description"], "Terminal")

        aside = {commands[1]["chord_id"]: {"shows": UNUSED_SHOWS, "penalty": 1.0, "at": 10}}
        led = slice_rows(
            commands, probabilities, ["Alacritty"], aside, now=10, actions=["opened wifi"],
        )[0]
        self.assertEqual(led[0]["description"], "Network")

    def test_exploration_leaves_out_the_five_names_and_waits_behind_them(self):
        terminal = self._command("1", "Terminal")
        network = self._command("2", "Network")
        lock = self._command("3", "Lock system")
        grok = self._command("4", "Grok")
        commands = [terminal, network, lock, grok]
        state = screen_state(None, [], [], ["Alacritty", "wifi"])
        stats = {
            "64:1": {"uses": 4, "hour": 1, "ago": 15},
            "64:3": {"uses": 2, "hour": 1, "ago": 1440},
        }
        request = build_split_request(state, commands, stats)
        self.assertEqual(set(request["questions"]), {"apps", "explore"})
        apps = request["questions"]["apps"]["criteria"]
        explore = request["questions"]["explore"]["criteria"]
        self.assertEqual(set(apps), {"1", "2"})
        self.assertEqual(set(explore), {"3", "4"})
        self.assertEqual(apps["1"]["uses"], 4)
        self.assertEqual(apps["1"]["hour"], 1)
        self.assertEqual(apps["1"]["ago"], 15)
        self.assertEqual(explore["3"]["uses"], 2)
        self.assertEqual(explore["3"]["ago"], 1440)
        for question in request["questions"].values():
            self.assertIn("forgotten", question["instructions"])
            self.assertIn("uses is every press kept", question["instructions"])
            self.assertNotIn("streak", question["instructions"])
        moved = build_split_request(screen_state(None, [], [], ["Alacritty", "menu"]), commands, stats)
        self.assertIn("2", moved["questions"]["explore"]["criteria"])
        self.assertNotIn("2", moved["questions"]["apps"]["criteria"])
        menus = [
            self._command("5", "Omarchy menu"),
            self._command("6", "Capture menu"),
            self._command("7", "System menu"),
        ]
        tmux = self._command("9", "Tmux keybindings")
        ranked = [terminal, tmux, *menus, grok]
        app_probs = {"1": 0.5, "9": 0.5, "5": 0.4, "6": 0.3, "7": 0.3}
        rows, pool = slice_rows(ranked, app_probs, ["Alacritty", "menu"], None, {"4": 0.9})
        shown = [row["description"] for row in rows]
        self.assertEqual(shown[:2], ["Terminal", "Tmux keybindings"])
        self.assertNotIn("Grok", shown)
        self.assertEqual(pool[-1]["description"], "Grok")
        resting = {
            command["chord_id"]: {"shows": UNUSED_SHOWS, "penalty": 1.0, "at": 10}
            for command in ranked if command["id"] != "4"
        }
        revealed = [row["description"] for row in slice_rows(ranked, app_probs, ["Alacritty", "menu"], resting, {"4": 0.9}, 10)[0]]
        self.assertEqual(revealed, ["Grok"])
        short = [row["description"] for row in slice_rows(commands, {"1": 1}, ["Alacritty"], None, {"3": 0.2, "4": 0.7})[0]]
        self.assertEqual(short[0], "Terminal")
        self.assertIn("Grok", short)
        self.assertNotIn("Network", short)
        body = {
            "answers": {
                "apps": {"probabilities": {"1": 0.6}, "confidence": 0.4},
                "explore": {"probabilities": {"3": 0.8}, "confidence": 0.2},
            }
        }
        self.assertTrue(ranking_ok(request, body))
        self.assertFalse(ranking_ok(request, {"answers": {"apps": body["answers"]["apps"]}}))
        stored = saved_rank(["Alacritty", "wifi"], request["questions"], body["answers"], 10)
        self.assertEqual(stored["probabilities"], {"1": 0.6})
        self.assertEqual(stored["explore"], {"3": 0.8})
        self.assertEqual(stored["confidence"], 0.4)
        self.assertEqual(stored["explore_confidence"], 0.2)
        lone = {"questions": {"next": {"type": "choice"}}}
        self.assertTrue(ranking_ok(lone, {"answers": {"next": {"probabilities": {"1": 1}, "confidence": 0}}}))
        self.assertFalse(ranking_ok(lone, {"answers": {"next": {"probabilities": {}, "confidence": True}}}))

    def test_more_than_255_shortcuts_are_capped(self):
        commands = [
            {"id": str(i), "arg": str(i), "description": f"Item {i}", "chord": "x", "chord_id": f"0:{i}"}
            for i in range(300)
        ]
        state = screen_state(None, [], [])
        capped = cap_choices(commands, state)
        self.assertEqual(len(capped), 255)
        self.assertEqual(len(cap_choices(commands[:10], state)), 10)


class SecretTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path("/tmp/shortcuts-ai-secret-test")
        if self.directory.exists():
            for child in self.directory.iterdir():
                child.unlink()
        else:
            self.directory.mkdir()
        self.tool = self.directory / "secret-tool"
        self.store = self.directory / "item"
        self.argv = self.directory / "argv"
        self.tool.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            f"root = Path({str(self.directory)!r})\n"
            "op = sys.argv[1]\n"
            "args = sys.argv[2:]\n"
            "if op == 'store' and args[:1] == ['--label']:\n"
            "    args = args[2:]\n"
            "root.joinpath('argv').write_text(' '.join(sys.argv[1:]))\n"
            "if op == 'store':\n"
            "    root.joinpath('item').write_text(sys.stdin.read())\n"
            "    sys.exit(0)\n"
            "if op == 'lookup':\n"
            "    item = root.joinpath('item')\n"
            "    if not item.exists():\n"
            "        sys.exit(1)\n"
            "    sys.stdout.write(item.read_text())\n"
            "    sys.exit(0)\n"
            "if op == 'clear':\n"
            "    root.joinpath('item').unlink(missing_ok=True)\n"
            "    sys.exit(0)\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        self.tool.chmod(0o755)

    def tearDown(self):
        for child in self.directory.iterdir():
            child.unlink()
        self.directory.rmdir()

    def test_a_key_is_stored_without_putting_it_on_the_command_line(self):
        self.assertTrue(store("secret-value", str(self.tool)))
        self.assertNotIn("secret-value", self.argv.read_text(encoding="utf-8"))
        self.assertEqual(lookup(str(self.tool)), "secret-value")
        self.assertTrue(clear(str(self.tool)))
        self.assertEqual(lookup(str(self.tool)), "")

    def test_a_leftover_file_is_moved_into_the_keyring_and_deleted(self):
        legacy = self.directory / "dir" / "jev.key"
        legacy.parent.mkdir()
        legacy.write_text("file-secret\n", encoding="utf-8")
        self.assertEqual(read_key(legacy, str(self.tool)), "file-secret")
        self.assertFalse(legacy.exists())
        self.assertFalse(legacy.parent.exists())
        self.assertEqual(lookup(str(self.tool)), "file-secret")
        again = self.directory / "dir" / "jev.key"
        again.parent.mkdir()
        again.write_text("other-secret\n", encoding="utf-8")
        self.assertEqual(read_key(again, str(self.tool)), "file-secret")
        self.assertFalse(again.exists())

    def test_a_blank_key_is_refused(self):
        self.assertFalse(store("  \n", str(self.tool)))
        self.assertEqual(lookup(str(self.tool)), "")


class RepoTests(unittest.TestCase):
    def test_the_api_key_is_not_in_the_repo(self):
        leaked = re.compile(r"apikey_[0-9a-fA-F]{8}")
        for path in ROOT.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(leaked.search(text), path)


if __name__ == "__main__":
    unittest.main()
