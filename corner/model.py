"""Pure decisions for the corner assistant.

Code builds a screen state and the full list of offerable shortcuts. One
Jev call ranks two pools for a set of five recent names: the shortcuts that
match those names, and every other shortcut for exploration. Later visits
cut the saved probabilities down to the card. Each option carries how often
it was used.
"""

from __future__ import annotations

import re
from typing import Iterable

# SHIFT=1, CTRL=4, ALT=8, SUPER=64. Lock modifiers (Num Lock, Caps) are ignored.
MOD_SHIFT = 1
MOD_CTRL = 4
MOD_ALT = 8
MOD_SUPER = 64
MOD_MASK = MOD_SHIFT | MOD_CTRL | MOD_ALT | MOD_SUPER

MOD_NAMES = (
    (MOD_SUPER, "SUPER"),
    (MOD_SHIFT, "SHIFT"),
    (MOD_CTRL, "CTRL"),
    (MOD_ALT, "ALT"),
)

# XKB keycodes at or above F1. The Hyprland hook uses the same cutoff so a
# plain letter is never written down, while volume and brightness keys are.
FUNCTION_KEYCODE = 67

LEARNED_USES = 6
LEARNED_SECONDS = 24 * 60 * 60
# Plain d and Shift+D are one shortcut: the corner debug bite.
CORNER_DEBUG_CHORD = "corner-debug"
CORNER_DEBUG_DESCRIPTION = "corner debug"
# Each question sends one pool. The API allows 255 options on a question.
CHOICE_LIMIT = 255
# Kept for the older twelve-name helper. The daemon does not use it.
SHORTLIST = 12
SUGGEST_COUNT = 5
# Presses of one suggested shortcut since the last rank. The next visit ranks again.
SUGGEST_REFRESH_USES = 2
# Each display without a use takes this much chance. Five puts it fully aside.
UNUSED_SHOWS = 5
# The loss fades to nothing over a day, measured from the last ignored display.
UNUSED_FORGET_SECONDS = 24 * 60 * 60
# (n * p_max - 1) / (n - 1). 0.05 is about 1.5x uniform for n=12.
# An even spread still stays off the card.
SHOW_CONFIDENCE = 0.05
DISCOVERY = 4  # unused shortcuts kept inside the same list of 12
FORGOTTEN_MINUTES = 1440  # a press this old can lead again; same snap as ago
REMIND = 2
TITLE_CHARS = 48
VISIBLE_WINDOWS = 6
RECENT_EVENTS = 8
# Class names and panel names, newest first. Titles stay on the focused window.
RECENT_APPS = 5
APP_CHARS = 32

# Everyday commands kept in the shortlist even when the window title shares
# no words with them, so an empty desktop still has something to rank.
FALLBACK_NEEDLES = (
    "terminal",
    "browser",
    "file manager",
    "close window",
    "full screen",
    "omarchy menu",
    "clipboard",
    "screenshot",
    "lock",
    "floating",
)

# Focused app class fragments, and words that make a command worth keeping.
APP_HINTS = (
    (("chromium", "chrome", "firefox", "brave"), ("browser", "tab", "zoom", "back", "forward")),
    (("alacritty", "foot", "kitty", "ghostty"), ("terminal", "tmux", "copy", "paste")),
    (("nautilus", "thunar"), ("file",)),
    (("spotify", "mpv", "vlc"), ("music", "media", "volume")),
)

_WORDS = re.compile(r"[a-z0-9]+")
_STOP = {
    "a", "an", "the", "to", "of", "and", "or", "on", "in", "for",
    "window", "omarchy", "toggle", "show", "with",
}
_BIND_CALL = re.compile(r"o\.bind\(")
_FOR_LOOP = re.compile(
    r"for\s+([A-Za-z_]\w*)\s*=\s*(\d+)\s*,\s*(\d+)\s*do\b(?P<body>.*?)\n[ \t]*end\b",
    re.S,
)
_LOCAL_ASSIGN = re.compile(r"local\s+([A-Za-z_]\w*)\s*=\s*(.+)")


def mask_mods(modmask: int) -> int:
    return int(modmask) & MOD_MASK


def chord_id(modmask: int, key: str) -> str:
    """Stable id for one physical shortcut, independent of Hyprland's lua index."""
    return f"{mask_mods(modmask)}:{key.casefold()}"


def pretty_chord(modmask: int, key: str) -> str:
    parts = [name for bit, name in MOD_NAMES if mask_mods(modmask) & bit]
    label = key if len(key) <= 2 else key.upper()
    parts.append(label)
    return " + ".join(parts)


# This keyboard has F1–F12. XKB names some missing vendor keys F13–F24.
# Keycode 201 is one of them: Omarchy binds it as a second menu chord, and
# the layout calls it F23.
_ABSENT_FUNCTION = re.compile(r"^f(?:1[3-9]|2[0-4])$", re.IGNORECASE)


def pressable_key(key: str) -> bool:
    """False for a keysym this keyboard does not have."""
    return _ABSENT_FUNCTION.match(key.strip()) is None


def clip(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def screen_state(
    active: dict | None,
    clients: Iterable[dict],
    recent: Iterable[str],
    apps: Iterable[str] | None = None,
    slots: Iterable[dict] | None = None,
) -> dict:
    """Compact, code-built picture of the screen. No pixels."""
    focused = _window_brief(active) if active else {
        "app": "", "title": "", "floating": False, "fullscreen": False,
    }
    workspace = ""
    if active and isinstance(active.get("workspace"), dict):
        workspace = str(active["workspace"].get("name") or active["workspace"].get("id") or "")
    ws_id = None
    if active and isinstance(active.get("workspace"), dict):
        ws_id = active["workspace"].get("id")

    visible = []
    for client in clients:
        if client is active:
            continue
        if not client.get("mapped") or client.get("hidden"):
            continue
        same = isinstance(client.get("workspace"), dict) and client["workspace"].get("id") == ws_id
        if not same and not client.get("pinned"):
            continue
        brief = _window_brief(client)
        if not brief["app"] and not brief["title"]:
            continue
        visible.append({"app": brief["app"], "title": brief["title"]})
        if len(visible) >= VISIBLE_WINDOWS:
            break

    just_did = [clip(item, 40) for item in list(recent)[-RECENT_EVENTS:] if str(item).strip()]
    # The focused class leads the list, so the app in front is recorded
    # even when focus did not change.
    recent_apps = remember_app(list(apps or []), focused["app"])
    return {
        "focused": focused,
        "workspace": workspace,
        "also_visible": visible,
        "just_did": just_did,
        "recent_apps": recent_apps,
        "bar": bar_apps(slots or []),
    }


def remember_app(apps: Iterable[str], name: str, limit: int = RECENT_APPS) -> list[str]:
    """Newest class name first. The same app is not listed twice, and titles are not accepted here."""
    clean = clip(str(name or ""), APP_CHARS)
    kept: list[str] = []
    seen: set[str] = set()
    if clean:
        kept.append(clean)
        seen.add(clean.casefold())
    for item in apps:
        if not isinstance(item, str):
            continue
        label = clip(item, APP_CHARS)
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        kept.append(label)
        if len(kept) >= limit:
            break
    return kept[:limit]


def remember_action(recent, text: str) -> bool:
    """Keep one new action. A repeat of the latest line is ignored.

    A terminal title that keeps changing is reported as activewindow, and the
    same focus line would otherwise push a bar click out of the short list.
    """
    clipped = clip(str(text or ""), 40)
    if not clipped:
        return False
    if recent and recent[-1] == clipped:
        return False
    recent.append(clipped)
    return True


def app_from_activewindow(data: str) -> str:
    """Hyprland sends `class,title`. Only the class is an app name."""
    return str(data or "").split(",", 1)[0].strip()


def history_document(
    apps: Iterable[str],
    just_did: Iterable[str],
    result: dict | None,
    rank: dict | None = None,
) -> dict:
    """The small file Jev's state is rebuilt from. Errors are left out."""
    document: dict = {
        "apps": remember_app(apps, ""),
        "just_did": [clip(item, 40) for item in list(just_did)[-RECENT_EVENTS:] if isinstance(item, str) and item.strip()],
    }
    kept = _kept_result(result)
    if kept:
        document["result"] = kept
    kept_rank = _kept_rank(rank)
    if kept_rank:
        document["rank"] = kept_rank
    return document


def load_history(data: object) -> dict:
    """Apps, recent lines, the last card, and the saved full ranking."""
    if not isinstance(data, dict):
        return {"apps": [], "just_did": [], "result": None, "rank": None}
    apps_in = data.get("apps") if isinstance(data.get("apps"), list) else []
    recent_in = data.get("just_did") if isinstance(data.get("just_did"), list) else []
    result = _kept_result(data.get("result"))
    if result is not None:
        result["from_disk"] = True
    return {
        "apps": remember_app(apps_in, ""),
        "just_did": [clip(item, 40) for item in recent_in[-RECENT_EVENTS:] if isinstance(item, str) and item.strip()],
        "result": result,
        "rank": _kept_rank(data.get("rank")),
    }


def _kept_result(result: object) -> dict | None:
    """The last card, small enough to keep. A failure is not kept."""
    if not isinstance(result, dict) or result.get("error"):
        return None
    fingerprint = str(result.get("fp") or "")
    if not fingerprint or len(fingerprint) > 64:
        return None
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        return None
    items = []
    for row in raw_items[:SUGGEST_COUNT]:
        if not isinstance(row, dict):
            continue
        arg = str(row.get("arg") or "")
        chord = clip(str(row.get("chord") or ""), 80)
        description = clip(str(row.get("description") or ""), 80)
        if not arg.isdigit() or not chord:
            continue
        items.append({
            "arg": arg,
            "chord": chord,
            "description": description,
            "percent": _percent(row.get("percent")),
        })
    try:
        at = float(result.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    kept: dict = {
        "items": items,
        "note": clip(str(result.get("note") or ""), 80),
        "fp": fingerprint,
        "at": at,
    }
    confidence = result.get("confidence")
    if not isinstance(confidence, bool):
        try:
            value = float(confidence)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = -1.0
        if 0 <= value <= 1:
            kept["confidence"] = value
    return kept


def _kept_probabilities(raw_probs: dict) -> dict[str, float]:
    """Positive shares only. A zero is the same as leaving the shortcut off."""
    probabilities: dict[str, float] = {}
    for key, raw in raw_probs.items():
        ident = str(key or "")
        if not ident.isdigit():
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number > 1:
            continue
        probabilities[ident] = number
        if len(probabilities) >= CHOICE_LIMIT:
            break
    return probabilities


def _kept_unit(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 1:
        return value
    return None


def _kept_rank(rank: object) -> dict | None:
    """Both pools. A ranking from before the exploration pool is kept, so a failed call can still draw the card.

    An empty map is kept once the exploration key exists. That visit already asked.
    """
    if not isinstance(rank, dict):
        return None
    raw_probs = rank.get("probabilities")
    if not isinstance(raw_probs, dict):
        return None
    raw_apps = rank.get("apps") if isinstance(rank.get("apps"), list) else []
    apps = remember_app([item for item in raw_apps if isinstance(item, str)], "")
    probabilities = _kept_probabilities(raw_probs)
    kept: dict = {"apps": apps, "probabilities": probabilities}
    try:
        kept["at"] = float(rank.get("at") or 0)
    except (TypeError, ValueError):
        kept["at"] = 0.0
    confidence = _kept_unit(rank.get("confidence"))
    if confidence is not None:
        kept["confidence"] = confidence
    if "explore" in rank and isinstance(rank.get("explore"), dict):
        kept["explore"] = _kept_probabilities(rank["explore"])
        explore_confidence = _kept_unit(rank.get("explore_confidence"))
        if explore_confidence is not None:
            kept["explore_confidence"] = explore_confidence
    if "offered" in rank:
        kept["offered"] = _kept_offered(rank.get("offered"))
    if not probabilities and "explore" not in kept:
        return None
    return kept


def _kept_offered(raw: object) -> list[str]:
    """Chord ids of the five shortcuts that rank put on the card."""
    if not isinstance(raw, list):
        return []
    offered: list[str] = []
    for item in raw:
        ident = str(item or "").strip()
        if not ident or len(ident) > 80 or ident in offered:
            continue
        offered.append(ident)
        if len(offered) >= SUGGEST_COUNT:
            break
    return offered


def same_used(left: Iterable[str] | None, right: Iterable[str] | None) -> bool:
    """True when two recent lists hold the same names, whatever the order."""
    def names(values: Iterable[str] | None) -> set[str]:
        found = set()
        for item in values or []:
            if not isinstance(item, str):
                continue
            label = item.strip().casefold()
            if label:
                found.add(label)
        return found

    return names(left) == names(right)


def suggestion_was_used(
    rank: dict | None,
    usage: Iterable[dict] | None,
    now: float,
    times: int = SUGGEST_REFRESH_USES,
) -> bool:
    """True when one shortcut from the saved card was pressed `times` since that rank."""
    if not isinstance(rank, dict) or times <= 0:
        return False
    offered = _kept_offered(rank.get("offered"))
    if not offered:
        return False
    try:
        since = float(rank.get("at") or 0)
    except (TypeError, ValueError):
        since = 0.0
    wanted = set(offered)
    counts: dict[str, int] = {}
    for event in usage or []:
        if not isinstance(event, dict):
            continue
        try:
            when = float(event["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if when <= since or when > now + 5:
            continue
        chord = str(event.get("chord") or "")
        if chord not in wanted:
            continue
        counts[chord] = counts.get(chord, 0) + 1
        if counts[chord] >= times:
            return True
    return False


def needs_full_rank(
    rank: dict | None,
    names: Iterable[str],
    usage: Iterable[dict] | None = None,
    now: float | None = None,
) -> bool:
    """A call is due when a pool is missing, the five names change, or a suggestion was pressed twice.

    The exploration pool is part of that call. A ranking saved before it
    existed is sent again once, even when the five names are the same.
    Presses before that ranking do not count toward the two.
    """
    if not isinstance(rank, dict):
        return True
    if not isinstance(rank.get("probabilities"), dict):
        return True
    if not isinstance(rank.get("explore"), dict):
        return True
    if not same_used(rank.get("apps"), names):
        return True
    # A ranking from before the card was recorded has nothing to count, so it is sent once.
    if "offered" not in rank:
        return True
    if usage is None or now is None:
        return False
    return suggestion_was_used(rank, usage, now)


# Bar popups share one layer name. The widget under the pointer tells them apart.
# The always-on surfaces are not clicks.
_LAYER_IGNORE = frozenset({
    "omarchy-bar",
    "omarchy-background",
    "omarchy-osd",
    "omarchy-notifications",
    "omarchy-keyboard-panel-dismiss",
    "omarchy-bar-drag-ghost",
    "omarchy-bar-move-ghost",
    "omarchy-lock-preview",
    "ignotas-mouse-sacrifice",
})
_LAYER_LABELS = {
    "omarchy-menu": "menu",
    "omarchy-clipboard": "clipboard",
    "omarchy-emojis": "emoji",
    "omarchy-network-qr": "wifi qr",
    "omarchy-reminders": "reminders",
    "omarchy-image-selector": "images",
    "omarchy-polkit": "password prompt",
}
_BAR_LABELS = {
    "omarchy.network": "wifi",
    "omarchy.audio": "audio",
    "omarchy.bluetooth": "bluetooth",
    "omarchy.power": "power",
    "omarchy.clock": "calendar",
    "omarchy.monitor": "display",
    "omarchy.weather": "weather",
    "omarchy.menu": "menu",
    "omarchy.workspaces": "workspaces",
    "omarchy.tray": "tray",
    "omarchy.agents": "agents",
    "omarchy.dropbox": "dropbox",
    "omarchy.tailscale": "tailscale",
}


def widget_at(slots: Iterable[dict], x: float, y: float) -> str:
    """Bar widget whose box contains the pointer. Hidden and empty boxes are skipped."""
    found = ""
    for slot in slots:
        if not isinstance(slot, dict) or slot.get("visible") is False:
            continue
        try:
            left = float(slot["x"])
            top = float(slot["y"])
            width = float(slot["width"])
            height = float(slot["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        if left <= x < left + width and top <= y < top + height:
            found = str(slot.get("id") or "")
    return found


def bar_label(widget_id: str) -> str:
    """Short name for one bar widget. Unknown ids keep their last segment."""
    key = str(widget_id or "").strip()
    if not key:
        return ""
    if key in _BAR_LABELS:
        return _BAR_LABELS[key]
    return key.split(".")[-1].replace("-", " ")[:24]


# A click says "wifi" or "calendar". The shortcut Omarchy registered says
# "Network" or "Clock", so scoring has to know both words.
_BAR_ALIASES = {
    "wifi": ("network",),
    "calendar": ("clock",),
    "display": ("monitor",),
}


def bar_apps(slots: Iterable[dict]) -> list[str]:
    """Every visible bar widget, left to right. Hidden and empty boxes are left out."""
    ordered: list[tuple[float, str]] = []
    for slot in slots:
        if not isinstance(slot, dict) or slot.get("visible") is False:
            continue
        try:
            left = float(slot["x"])
            width = float(slot["width"])
            height = float(slot["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        label = bar_label(str(slot.get("id") or ""))
        if label:
            ordered.append((left, label))
    ordered.sort(key=lambda item: item[0])
    names: list[str] = []
    seen: set[str] = set()
    for _, label in ordered:
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(label)
        if len(names) >= 16:
            break
    return names


def used_layer_name(namespace: str, widget_id: str = "") -> str:
    """Panel name that joins the same recent list as an app.

    Empty when this layer is not a use. A bar popup with no widget is empty
    here; the action line can still say "a panel".
    """
    name = str(namespace or "").split(",", 1)[0].strip()
    if not name or name in _LAYER_IGNORE or name.startswith("ignotas-"):
        return ""
    if name == "omarchy-keyboard-panel":
        return bar_label(widget_id) if widget_id else ""
    return _LAYER_LABELS.get(name, "")


def layer_phrase(action: str, namespace: str, widget_id: str = "") -> str:
    """One short line for `just_did`, or empty when the layer is not a click.

    `omarchy-keyboard-panel` is every bar popup. `widget_id` says which button.
    """
    label = used_layer_name(namespace, widget_id)
    if label:
        return f"{action} {label}"
    name = str(namespace or "").split(",", 1)[0].strip()
    if name == "omarchy-keyboard-panel":
        return f"{action} a panel"
    return ""


def _window_brief(window: dict | None) -> dict:
    if not window:
        return {"app": "", "title": "", "floating": False, "fullscreen": False}
    fullscreen = window.get("fullscreen")
    return {
        "app": clip(window.get("class") or window.get("initialClass") or "", 40),
        "title": clip(window.get("title") or "", TITLE_CHARS),
        "floating": bool(window.get("floating")),
        "fullscreen": bool(fullscreen),
    }


_HOUR = 60 * 60
# Minutes. Coarse on purpose: a finer age would change the cached request.
# 1440 is a day, 4320 is three days, 10080 is a week, 43200 is 30 days,
# 129600 is 90 days. Older than that stays on the last step.
_AGO_EDGES = (5, 15, 60, 180, 720, 1440, 4320, 10080, 43200, 129600)


def _ago_bucket(age_seconds: float) -> int:
    minutes = max(0.0, age_seconds) / 60
    bucket = 0
    for edge in _AGO_EDGES:
        if minutes >= edge:
            bucket = edge
        else:
            break
    return bucket


def chord_stats(events: Iterable[dict], now: float, window: int | None = None) -> dict[str, dict]:
    """Per chord: presses kept, presses in the last hour, and a coarse age.

    `window` limits the count. None keeps every press. `ago` is minutes since
    the latest press, snapped to the steps in `_AGO_EDGES`.
    """
    counts: dict[str, int] = {}
    hours: dict[str, int] = {}
    latest: dict[str, float] = {}
    cutoff = None if window is None else now - window
    hour_cut = now - _HOUR
    for event in events:
        try:
            when = float(event["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if when > now + 5 or (cutoff is not None and when < cutoff):
            continue
        chord = str(event.get("chord") or "")
        if not chord:
            continue
        counts[chord] = counts.get(chord, 0) + 1
        if when >= hour_cut:
            hours[chord] = hours.get(chord, 0) + 1
        if chord not in latest or when > latest[chord]:
            latest[chord] = when
    stats: dict[str, dict] = {}
    for chord, uses in counts.items():
        row: dict = {"uses": uses, "ago": _ago_bucket(now - latest[chord])}
        hour = hours.get(chord, 0)
        if hour:
            row["hour"] = hour
        stats[chord] = row
    return stats


def learned_chords(events: Iterable[dict], now: float, uses: int = LEARNED_USES, window: int = LEARNED_SECONDS) -> set[str]:
    """Chords pressed at least `uses` times in the window.

    The card does not drop these. Jev sees the counts and decides.
    """
    return {
        chord
        for chord, row in chord_stats(events, now, window).items()
        if int(row["uses"]) >= uses
    }


def seed_corner_debug(
    existing: Iterable[dict],
    presses: int,
    when: float,
    now: float,
    window: int | None = None,
) -> list[dict]:
    """Count corner bites already on disk, once.

    The log has no per-press time. The file's mtime stands in for those presses.
    A later start sees the chord and does not add them again. `window` is ignored;
    old bites stay in the history.
    """
    del window
    rows = list(existing)
    if presses <= 0:
        return rows
    if any(str(row.get("chord") or "") == CORNER_DEBUG_CHORD for row in rows):
        return rows
    if when > now + 5:
        return rows
    rows.extend({"t": when, "chord": CORNER_DEBUG_CHORD} for _ in range(presses))
    return rows


def strip_lua_comments(source: str) -> str:
    out: list[str] = []
    i = 0
    in_string = False
    while i < len(source):
        if not in_string and source.startswith("--[[", i):
            end = source.find("]]", i + 4)
            i = len(source) if end < 0 else end + 2
            continue
        if not in_string and source.startswith("--", i):
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
            continue
        char = source[i]
        if char == '"':
            in_string = not in_string
        out.append(char)
        i += 1
    return "".join(out)


def _eval_expr(expr: str, env: dict[str, str]) -> str | None:
    parts = re.split(r"\s*\.\.\s*", expr.strip().rstrip(","))
    if not parts:
        return None
    chunks: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
            chunks.append(part[1:-1])
            continue
        if part in env:
            chunks.append(env[part])
            continue
        call = re.fullmatch(r"tostring\(\s*([A-Za-z_]\w*)\s*(?:\+\s*(\d+))?\s*\)", part)
        if call and call.group(1) in env and env[call.group(1)].isdigit():
            base = int(env[call.group(1)])
            chunks.append(str(base + int(call.group(2) or 0)))
            continue
        return None
    return "".join(chunks)


def _rewrite_line(line: str, env: dict[str, str]) -> str:
    """Resolve tostring() and string concatenation that a bind loop uses."""

    def repl_tostring(match: re.Match[str]) -> str:
        name = match.group(1)
        extra = match.group(2)
        if name in env and str(env[name]).isdigit():
            return '"' + str(int(env[name]) + int(extra or 0)) + '"'
        return match.group(0)

    line = re.sub(
        r"tostring\(\s*([A-Za-z_]\w*)\s*(?:\+\s*(\d+))?\s*\)",
        repl_tostring,
        line,
    )
    while True:
        collapsed = re.sub(r'"([^"]*)"\s*\.\.\s*"([^"]*)"', r'"\1\2"', line)
        if collapsed == line:
            break
        line = collapsed

    def repl_var(match: re.Match[str]) -> str:
        name = match.group(2)
        if name in env:
            return f'"{match.group(1)}{env[name]}"'
        return match.group(0)

    return re.sub(r'"([^"]*)"\s*\.\.\s*([A-Za-z_]\w*)', repl_var, line)


def _expand_loops(source: str) -> str:
    def repl(match: re.Match[str]) -> str:
        var = match.group(1)
        start = int(match.group(2))
        stop = int(match.group(3))
        body = match.group("body")
        rendered: list[str] = []
        for value in range(start, stop + 1):
            env = {var: str(value)}
            for line in body.splitlines():
                line = _rewrite_line(line, env)
                assign = _LOCAL_ASSIGN.match(line.strip())
                if assign:
                    got = _eval_expr(assign.group(2), env)
                    if got is not None:
                        env[assign.group(1)] = got
                rendered.append(line)
        return "\n".join(rendered) + "\n"

    previous = None
    expanded = source
    while previous != expanded:
        previous = expanded
        expanded = _FOR_LOOP.sub(repl, expanded)
    return expanded


def _bind_args(source: str, start: int) -> tuple[str, str] | None:
    """First two arguments of an o.bind( call. `start` points at the '('."""
    if start >= len(source) or source[start] != "(":
        return None
    # The '(' we were given is the call itself. Counting it would hide every
    # comma inside the call, including the ones between the key and the label.
    depth = 0
    in_string = False
    escaped = False
    args: list[str] = []
    buf: list[str] = []
    i = start + 1
    while i < len(source):
        char = source[i]
        if in_string:
            buf.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            buf.append(char)
        elif char == "(":
            depth += 1
            buf.append(char)
        elif char == ")":
            if depth == 0:
                args.append("".join(buf).strip())
                break
            depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            args.append("".join(buf).strip())
            buf = []
            # The third argument is only read for a web app host. Stop there.
            if len(args) == 3:
                break
        else:
            buf.append(char)
        i += 1
    if len(args) < 2:
        return None
    return tuple(args)


_WEB_HOST = re.compile(r"""webapp\s*=\s*["']https?://([^/"'\s]+)""")


def web_host(spec: str) -> str:
    """Site only. `app.hey.com/calendar/weeks/` becomes `hey.com`."""
    match = _WEB_HOST.search(spec or "")
    if not match:
        return ""
    host = match.group(1).lower().rstrip(".")
    for prefix in ("www.", "app.", "web."):
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
            break
    return host[:24]


def parse_bind_keys(source: str) -> list[dict]:
    """Recover key strings Hyprland omits for code: binds.

    Returns rows of {keys, description}. Literal binds and the small
    `for i = a, b do o.bind(... tostring(i + n) ...)` loops used by Omarchy
    are both understood. Anything fancier is left unnamed.
    """
    text = _expand_loops(strip_lua_comments(source))
    found: list[dict] = []
    for match in _BIND_CALL.finditer(text):
        parsed = _bind_args(text, match.end() - 1)
        if not parsed:
            continue
        keys = _eval_expr(parsed[0], {})
        description = _eval_expr(parsed[1], {})
        if not keys or not description:
            continue
        row = {"keys": keys, "description": description}
        host = web_host(parsed[2]) if len(parsed) > 2 else ""
        if host:
            row["web"] = host
        found.append(row)
    return found


def split_keys(keys: str) -> tuple[int, str | None, int | None]:
    """'SUPER + code:20' -> (modmask, None, 20). 'SUPER + RETURN' -> (64, 'RETURN', None)."""
    modmask = 0
    key = None
    code = None
    for raw in keys.split("+"):
        part = raw.strip()
        if not part:
            continue
        upper = part.upper()
        if upper == "SUPER":
            modmask |= MOD_SUPER
        elif upper == "SHIFT":
            modmask |= MOD_SHIFT
        elif upper in {"CTRL", "CONTROL"}:
            modmask |= MOD_CTRL
        elif upper == "ALT":
            modmask |= MOD_ALT
        elif upper.startswith("CODE:"):
            number = upper[5:]
            if number.isdigit():
                code = int(number)
        elif key is None:
            key = part
    return modmask, key, code


def commands_from_binds(
    binds: Iterable[dict],
    declared: Iterable[dict],
    keysym_for_code,
    pressable: set[str] | None = None,
) -> list[dict]:
    """Live Hyprland binds, with a chord id that survives a config reload.

    `keysym_for_code(n)` returns the keysym name for an XKB keycode, or "".
    `pressable` is the keysym names the keyboard can produce. A chord whose
    key is not in that set is dropped instead of shown under a made-up name.
    Mouse, lid, locked, and undescribed binds are dropped. Locked is the
    Hyprland flag on hardware keys that still work on the lock screen.
    """
    declared_code: dict[tuple[str, int], int] = {}
    declared_web: dict[tuple[str, int], str] = {}
    for row in declared:
        modmask, _key, code = split_keys(row["keys"])
        modmask = mask_mods(modmask)
        host = str(row.get("web") or "")
        if host:
            declared_web[(row["description"], modmask)] = host
        if code is None:
            continue
        declared_code[(row["description"], modmask)] = code

    commands = []
    seen: set[tuple[str, str]] = set()
    for bind in binds:
        if bind.get("mouse") or bind.get("catch_all"):
            continue
        if bind.get("submap") or bind.get("locked"):
            continue
        description = str(bind.get("description") or "").strip()
        if not description:
            continue
        key = str(bind.get("key") or "").strip()
        if key.startswith("mouse") or key.startswith("switch:"):
            continue
        modmask = mask_mods(int(bind.get("modmask") or 0))
        code = declared_code.get((description, modmask))
        if key:
            canonical = key
        elif code is not None:
            canonical = keysym_for_code(code) or f"code:{code}"
        else:
            canonical = ""
        if not canonical or not pressable_key(canonical):
            continue
        if pressable is not None and canonical.casefold() not in pressable:
            continue
        ident = chord_id(modmask, canonical)
        if description.casefold() == CORNER_DEBUG_DESCRIPTION:
            ident = CORNER_DEBUG_CHORD
        # Two lua callbacks can share one chord. Keep both commands.
        # Repeated binds of the same chord and description collapse to one.
        marker = (ident, description)
        if marker in seen:
            continue
        seen.add(marker)
        label = key or canonical
        command = {
            "id": str(bind.get("arg") or ""),
            "arg": str(bind.get("arg") or ""),
            "description": description,
            "chord": pretty_chord(modmask, label),
            "chord_id": ident,
        }
        host = declared_web.get((description, modmask), "")
        if host:
            command["web"] = host
        commands.append(command)
    return [command for command in commands if command["arg"].isdigit()]


def _tokens(text: str) -> set[str]:
    return {word for word in _WORDS.findall(text.lower()) if word not in _STOP and len(word) > 1}


def matches_used(command: dict, names: Iterable[str]) -> bool:
    """True when the shortcut fits one of the five remembered names.

    A window class uses the same hints as scoring. A panel uses the bar
    aliases, so wifi matches Network and calendar matches Clock.
    """
    description = str(command.get("description") or "")
    described = _tokens(description)
    low_description = description.lower()
    for name in names:
        if not isinstance(name, str):
            continue
        label = name.strip()
        if not label:
            continue
        low = label.casefold()
        words = _tokens(label)
        for extra in _BAR_ALIASES.get(low, ()):
            words |= _tokens(extra)
        if words & described:
            return True
        for classes, hints in APP_HINTS:
            if any(piece in low for piece in classes) and any(hint in low_description for hint in hints):
                return True
    return False


def _score(command: dict, state: dict) -> int:
    blob = " ".join([
        state.get("focused", {}).get("app", ""),
        state.get("focused", {}).get("title", ""),
        " ".join(f"{item.get('app', '')} {item.get('title', '')}" for item in state.get("also_visible") or []),
        " ".join(state.get("just_did") or []),
        " ".join(str(name) for name in state.get("recent_apps") or []),
    ])
    score = len(_tokens(blob) & _tokens(command["description"])) * 10
    app = str(state.get("focused", {}).get("app") or "").lower()
    description = command["description"].lower()
    for classes, hints in APP_HINTS:
        if any(piece in app for piece in classes) and any(hint in description for hint in hints):
            score += 3
    # One point for an app they just left, so its shortcut can still be offered.
    for name in state.get("recent_apps") or []:
        previous = str(name or "").lower()
        if not previous or previous == app:
            continue
        if any(
            any(piece in previous for piece in classes) and any(hint in description for hint in hints)
            for classes, hints in APP_HINTS
        ):
            score += 1
            break
    if state.get("focused", {}).get("fullscreen") and "full screen" in description:
        score += 1
    # One point when the shortcut is one of the panels on the bar. The whole
    # bar is listed, so this is not limited to wifi and the calendar.
    described = _tokens(description)
    for name in state.get("bar") or []:
        words = _tokens(str(name))
        for extra in _BAR_ALIASES.get(str(name).casefold(), ()):
            words |= _tokens(extra)
        if words & described:
            score += 1
            break
    return score


def _info(command: dict, stats: dict | None) -> dict:
    info = (stats or {}).get(command.get("chord_id")) or {}
    return info if isinstance(info, dict) else {}


def _stat(command: dict, stats: dict | None, field: str) -> int:
    try:
        number = int(_info(command, stats).get(field) or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _uses(command: dict, stats: dict | None) -> int:
    return _stat(command, stats, "uses")


def _by_screen(state: dict):
    return lambda command: (-_score(command, state), command["description"])


def _sorted_unused(commands, state: dict, stats: dict | None) -> list:
    pool = [
        command for command in commands
        if str(command.get("arg") or "").isdigit() and _uses(command, stats) == 0
    ]
    pool.sort(key=_by_screen(state))
    return pool


def _take(pool, seen: set, limit: int) -> list:
    room = []
    for command in pool:
        arg = command["arg"]
        if arg in seen:
            continue
        seen.add(arg)
        room.append(command)
        if len(room) >= limit:
            break
    return room


def cap_choices(commands: Iterable[dict], state: dict, limit: int = CHOICE_LIMIT) -> list[dict]:
    """The API allows 255 options. Under that, the bind order stays."""
    command_list = [command for command in commands if isinstance(command, dict)]
    if len(command_list) <= limit:
        return command_list
    ranked = sorted(command_list, key=_by_screen(state))
    return ranked[:limit]


def shortlist(commands: Iterable[dict], state: dict, stats: dict | None = None, limit: int = SHORTLIST) -> list[dict]:
    """One Jev choice. Unused and forgotten shortcuts stay in the same list."""
    command_list = list(commands)
    picked = _shortlist(command_list, state, limit)
    picked = _with_discovery(picked, command_list, state, stats, limit)
    return _with_reminders(picked, command_list, state, stats, limit)


def _with_discovery(
    picked: list[dict],
    commands: list[dict],
    state: dict,
    stats: dict | None,
    limit: int,
) -> list[dict]:
    if not stats or not picked:
        return picked
    if any(_uses(command, stats) == 0 for command in picked):
        return picked
    pool = _sorted_unused(commands, state, stats)
    if not pool:
        least = min(_uses(command, stats) for command in commands)
        if least >= min(_uses(command, stats) for command in picked):
            return picked
        pool = [command for command in commands if _uses(command, stats) == least]
        pool.sort(key=_by_screen(state))
    room = _take(pool, {command["arg"] for command in picked}, DISCOVERY)
    if not room:
        return picked
    keep = max(0, limit - len(room))
    head = []
    held = {command["arg"] for command in room}
    for command in picked:
        if command["arg"] in held:
            continue
        head.append(command)
        if len(head) >= keep:
            break
    return (head + room)[:limit]


def _ago_of(command: dict, stats: dict | None) -> int:
    return _stat(command, stats, "ago")


def _with_reminders(
    picked: list[dict],
    commands: list[dict],
    state: dict,
    stats: dict | None,
    limit: int,
) -> list[dict]:
    """Keep a forgotten shortcut in the same list, so Jev can bring it back."""
    if not stats or not picked:
        return picked
    forgotten = [
        command for command in commands
        if _uses(command, stats) > 0 and _ago_of(command, stats) >= FORGOTTEN_MINUTES
    ]
    if not forgotten:
        return picked
    if any(command["arg"] in {item["arg"] for item in forgotten} for command in picked):
        return picked
    forgotten.sort(key=lambda command: (
        -_score(command, state),
        -_ago_of(command, stats),
        command["description"],
    ))
    room = _take(forgotten, {command["arg"] for command in picked}, REMIND)
    if not room:
        return picked
    protected = []
    recent = []
    for command in picked:
        if _uses(command, stats) == 0 or _ago_of(command, stats) >= FORGOTTEN_MINUTES:
            protected.append(command)
        else:
            recent.append(command)
    slots = max(0, limit - len(room) - len(protected))
    return (recent[:slots] + protected + room)[:limit]


def _shortlist(commands: list[dict], state: dict, limit: int) -> list[dict]:
    ranked = sorted(commands, key=_by_screen(state))
    primary = [command for command in ranked if _score(command, state) > 0][: limit - 4]
    chosen = {command["arg"] for command in primary}
    extras: list[dict] = []
    for needle in FALLBACK_NEEDLES:
        for command in ranked:
            if command["arg"] in chosen:
                continue
            if needle in command["description"].lower():
                extras.append(command)
                chosen.add(command["arg"])
                break
        if len(primary) + len(extras) >= limit:
            break
    picked = (primary + extras)[:limit]
    if len(picked) < min(12, limit):
        for command in ranked:
            if command["arg"] in chosen:
                continue
            picked.append(command)
            chosen.add(command["arg"])
            if len(picked) >= min(12, limit):
                break
    return picked[:limit]


def _option_facts(command: dict, stats: dict | None) -> dict:
    info = _info(command, stats)
    uses = _stat(command, stats, "uses")
    fact = {"chord": command["chord"], "does": command["description"], "uses": uses}
    host = str(command.get("web") or "")
    if host:
        fact["web"] = host
    if not uses:
        return fact
    hour = _stat(command, stats, "hour")
    if hour > 0:
        fact["hour"] = hour
    if "ago" in info:
        fact["ago"] = _stat(command, stats, "ago")
    return fact


# Shared by every Choice. uses, hour, and ago stay on the criteria themselves.
_RANK_FACTS = (
    "uses is every press kept, hour is presses in the last hour, "
    "and ago is minutes since the last press, snapped to "
    "0, 5, 15, 60, 180, 720, 1440, 4320, 10080, 43200, or 129600. "
    "A large ago means they knew this shortcut and may have forgotten it, "
    "so it can lead again when the screen fits. "
    "`just_did` includes recent window changes and bar actions such as `opened wifi` or `opened calendar`. "
    "`bar` is every widget on the top bar, left to right, not only wifi and the calendar. "
    "`web` is the site a shortcut opens. The same name without `web` is the local panel. "
    "`recent_apps` is the last apps used, newest first, with no titles. "
    "Give a shortcut used heavily and recently almost no probability "
    "unless the screen clearly needs it again. "
    "When those are the ones they know, put the probability on unused "
    "shortcuts that fit the screen, so a few new ones lead. "
    "If none of the shortcuts fit, spread probability evenly."
)


def _choice(lead: str, commands: list[dict], stats: dict | None) -> dict:
    return {
        "type": "choice",
        "instructions": f"{lead}{_RANK_FACTS}",
        "criteria": {command["id"]: _option_facts(command, stats) for command in commands},
    }


def partition_commands(commands: Iterable[dict], names: Iterable[str]) -> tuple[list[dict], list[dict]]:
    """Shortcuts that match the five names, then every other offerable shortcut."""
    matched: list[dict] = []
    explore: list[dict] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        if matches_used(command, names):
            matched.append(command)
        else:
            explore.append(command)
    return matched, explore


def build_request(state: dict, commands: list[dict], stats: dict | None = None, model: str = "jev-latest") -> dict:
    """One Choice over the list it is given. Criteria carry each shortcut's own counts."""
    return {
        "model": model,
        "state": state,
        "questions": {
            "next": _choice("Which shortcut should be offered next? ", list(commands), stats),
        },
    }


def build_split_request(state: dict, commands: Iterable[dict], stats: dict | None = None, model: str = "jev-latest") -> dict:
    """Two Choices in one call. The exploration pool leaves out the five names' shortcuts.

    A side with nothing to rank is omitted. Each side stays within the option limit.
    """
    names = list(state.get("recent_apps") or [])
    matched, explore = partition_commands(commands, names)
    questions: dict = {}
    app_commands = cap_choices(matched, state)
    if app_commands:
        questions["apps"] = _choice(
            "Which shortcut for the recent apps and panels should be offered next? ",
            app_commands,
            stats,
        )
    explore_commands = cap_choices(explore, state)
    if explore_commands:
        questions["explore"] = _choice(
            "Which shortcut should be offered for exploration? None of these belong to the recent apps and panels. ",
            explore_commands,
            stats,
        )
    return {"model": model, "state": state, "questions": questions}


def saved_rank(names: Iterable[str], questions: dict, answers: dict, at: float) -> dict:
    """The two pools to store. A question that was not asked keeps an empty map."""
    saved: dict = {
        "apps": [name for name in names if isinstance(name, str)],
        "probabilities": {},
        "explore": {},
        "at": float(at),
    }
    if "apps" in questions and isinstance(answers.get("apps"), dict):
        answer = answers["apps"]
        raw = answer.get("probabilities")
        saved["probabilities"] = raw if isinstance(raw, dict) else {}
        saved["confidence"] = float(answer["confidence"])
    if "explore" in questions and isinstance(answers.get("explore"), dict):
        answer = answers["explore"]
        raw = answer.get("probabilities")
        saved["explore"] = raw if isinstance(raw, dict) else {}
        saved["explore_confidence"] = float(answer["confidence"])
        if "confidence" not in saved:
            saved["confidence"] = saved["explore_confidence"]
    return saved


def rank(commands: list[dict], probabilities: dict, confidence: object, limit: int = SUGGEST_COUNT) -> list[dict]:
    """Top probabilities at or above SHOW_CONFIDENCE. A zero stays off the card."""
    if isinstance(confidence, bool):
        return []
    try:
        sure = float(confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if sure < SHOW_CONFIDENCE or not isinstance(probabilities, dict):
        return []
    scored = []
    for command in commands:
        try:
            probability = float(probabilities.get(command["id"], 0) or 0)
        except (TypeError, ValueError):
            probability = 0.0
        if probability > 0:
            scored.append((probability, command))
    scored.sort(key=lambda item: (-item[0], item[1]["description"]))
    return [{**command, "p": probability} for probability, command in scored[:limit]]


# docs.typesafe.ai/models — Jev 1.13. Output tokens are free.
INPUT_USD_PER_MILLION = 0.042


def _count(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def jev_usage(body: dict) -> tuple[int, int]:
    """Input and output tokens from a system-one response. Missing counts are zero."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    return _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))


def jev_cost(input_tokens: int) -> float:
    """Dollars. Output tokens are not charged."""
    return _count(input_tokens) * INPUT_USD_PER_MILLION / 1_000_000


def add_jev_spend(total: dict | None, input_tokens: int, output_tokens: int) -> dict:
    base = total if isinstance(total, dict) else {}
    incoming = _count(base.get("input_tokens")) + _count(input_tokens)
    return {
        "calls": _count(base.get("calls")) + 1,
        "input_tokens": incoming,
        "output_tokens": _count(base.get("output_tokens")) + _count(output_tokens),
        "usd": round(jev_cost(incoming), 10),
    }


def format_usd(usd: float) -> str:
    if usd <= 0:
        return "$0"
    if usd < 0.01:
        cents = f"{usd * 100:.4f}".rstrip("0").rstrip(".")
        return f"{cents}¢"
    return f"${usd:.2f}"


def debug_line(enabled: bool, spend: str) -> str:
    """The card hides the cost until debug is on."""
    if not enabled:
        return ""
    if spend:
        return f"debug · {spend}"
    return "debug"


def spend_line(total: dict | None, this_input: int = 0, confidence: object = None) -> str:
    """One quiet line for the card. A cached open passes this_input 0.

    When debug is on, the Choice confidence is appended so the gate is visible.
    """
    if not isinstance(total, dict) or _count(total.get("calls")) <= 0:
        return ""
    calls = _count(total.get("calls"))
    overall = format_usd(float(total.get("usd") or jev_cost(_count(total.get("input_tokens")))))
    if this_input > 0:
        line = f"Jev {format_usd(jev_cost(this_input))} · {overall} over {calls}"
    else:
        line = f"Jev {overall} over {calls}"
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return line
    number = float(confidence)
    if number < 0 or number > 1:
        return line
    return f"{line} · {number:.2f}"


def rebind_rows(rows: Iterable[dict], commands: Iterable[dict]) -> list[dict] | None:
    """Point a saved card at the shortcuts Hyprland has now.

    None means the command list was empty, so the old rows are left alone.
    """
    live: dict[tuple[str, str], str] = {}
    for command in commands:
        if not isinstance(command, dict):
            continue
        arg = str(command.get("arg") or "")
        if not arg.isdigit():
            continue
        live.setdefault((str(command.get("chord") or ""), str(command.get("description") or "")), arg)
    if not live:
        return None
    rebound = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        arg = live.get((str(row.get("chord") or ""), str(row.get("description") or "")), "")
        if not arg:
            continue
        rebound.append({
            "arg": arg,
            "chord": str(row.get("chord") or ""),
            "description": str(row.get("description") or ""),
            "percent": _percent(row.get("percent")),
        })
    return rebound


def _percent(value: object) -> int | str:
    """A blank percent is a discovery row. Jev rows stay 0–100."""
    if value == "" or value is None:
        return ""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, number))


def _panel_row(command: dict, percent: int | str) -> dict:
    return {
        "arg": str(command["arg"]),
        "chord": command["chord"],
        "description": command["description"],
        "percent": percent,
    }


def discovery_rows(
    commands: Iterable[dict],
    state: dict,
    stats: dict | None = None,
    limit: int = SUGGEST_COUNT,
) -> list[dict]:
    """Never-pressed shortcuts, best screen match first. No probability."""
    return [
        _panel_row(command, "")
        for command in _take(_sorted_unused(commands, state, stats), set(), limit)
    ]


def suggestion_rows(ranked: list[dict]) -> list[dict]:
    """What the panel renders. Percents are for the person, not for Jev."""
    return [_panel_row(command, int(round(command["p"] * 100))) for command in ranked]


def unused_penalty(unused: dict | None, chord_id: object, now: float | None = None) -> float:
    """Chance already lost, from 0 to 1. A day after the last ignore it is 0."""
    row = (unused or {}).get(str(chord_id or ""))
    if not isinstance(row, dict):
        return 0.0
    try:
        stored = float(row.get("penalty") or 0)
        at = float(row.get("at") or 0)
    except (TypeError, ValueError):
        return 0.0
    if isinstance(row.get("penalty"), bool) or stored <= 0:
        return 0.0
    if now is None:
        age = 0.0
    else:
        age = max(0.0, float(now) - at)
    if age >= UNUSED_FORGET_SECONDS:
        return 0.0
    return min(1.0, stored * (1 - age / UNUSED_FORGET_SECONDS))


def kept_unused(data: object) -> dict:
    """Well-formed rows from the unused file. Expired rows are left for the clock."""
    if not isinstance(data, dict):
        return {}
    kept: dict = {}
    for key, raw in data.items():
        ident = str(key or "").strip()
        if not ident or len(ident) > 80 or not isinstance(raw, dict):
            continue
        if isinstance(raw.get("penalty"), bool) or isinstance(raw.get("shows"), bool):
            continue
        try:
            penalty = float(raw.get("penalty"))
            at = float(raw.get("at"))
            shows = int(raw.get("shows") or 0)
        except (TypeError, ValueError):
            continue
        if penalty <= 0 or penalty > 1 or shows < 0:
            continue
        kept[ident] = {"shows": shows, "penalty": penalty, "at": at}
    return kept


def unused_from_shows(shows: dict, now: float) -> dict:
    """The old visit counts become a penalty. Five or more starts fully aside."""
    nxt: dict = {}
    if not isinstance(shows, dict):
        return nxt
    for key, value in shows.items():
        ident = str(key or "").strip()
        if not ident or isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        nxt[ident] = {
            "shows": value,
            "penalty": min(1.0, value / UNUSED_SHOWS),
            "at": float(now),
        }
    return nxt


def note_unused(
    unused: dict | None,
    visible_ids: Iterable[str],
    used_ids: Iterable[str] = (),
    now: float = 0,
) -> dict:
    """One ignored display takes another fifth. A use clears that shortcut.

    Rows that were not on this card keep fading from their own last ignore.
    """
    used = {str(item) for item in used_ids if str(item)}
    visible: list[str] = []
    seen: set[str] = set()
    for item in visible_ids:
        ident = str(item or "").strip()
        if not ident or ident in used or ident in seen:
            continue
        seen.add(ident)
        visible.append(ident)
    previous = unused or {}
    nxt: dict = {}
    for ident, row in previous.items():
        if ident in used or ident in seen:
            continue
        if unused_penalty(previous, ident, now) <= 0:
            continue
        cleaned = kept_unused({ident: row})
        if cleaned:
            nxt.update(cleaned)
    step = 1 / UNUSED_SHOWS
    for ident in visible:
        left = unused_penalty(previous, ident, now)
        shows = 1
        row = previous.get(ident)
        if left > 0 and isinstance(row, dict):
            try:
                shows = int(row.get("shows") or 0) + 1
            except (TypeError, ValueError):
                shows = 1
        penalty = left + step
        if penalty >= 1 - 1e-9:
            penalty = 1.0
        else:
            penalty = round(penalty, 6)
        nxt[ident] = {"shows": shows, "penalty": penalty, "at": float(now)}
    return nxt


def recent_use_penalty(stats: dict | None, chord_id: object) -> float:
    """Presses in the last hour. Five sets the shortcut aside until they age out.

    A press clears the ignored-display count. This penalty stays, so using a
    shortcut a lot cannot put it back at the top of the card.
    """
    row = (stats or {}).get(str(chord_id or ""))
    if not isinstance(row, dict) or isinstance(row.get("hour"), bool):
        return 0.0
    try:
        hour = int(row.get("hour") or 0)
    except (TypeError, ValueError):
        return 0.0
    if hour <= 0:
        return 0.0
    return min(1.0, hour / UNUSED_SHOWS)


def offer_rows(
    ranked: Iterable[dict],
    unused: dict | None = None,
    limit: int = SUGGEST_COUNT,
    now: float | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """The card, after ignored displays and recent presses have taken chance away.

    A shortcut at full penalty yields to the next one. When every shortcut
    is fully aside, the card keeps the original order rather than going blank.
    """
    groups: dict[str, list[dict]] = {"app": [], "explore": []}
    plain: list[dict] = []
    for command in ranked:
        if not isinstance(command, dict):
            continue
        plain.append(command)
        try:
            base = float(command.get("p") or 0)
        except (TypeError, ValueError):
            base = 0.0
        ident = command.get("chord_id")
        ignored = unused_penalty(unused, ident, now)
        used = recent_use_penalty(stats, ident)
        score = base * (1 - ignored) * (1 - used)
        if score <= 0:
            continue
        item = dict(command)
        item["p"] = score
        group = "explore" if command.get("pool") == "explore" else "app"
        groups[group].append(item)
    def _by_chance(command: dict) -> tuple:
        return (
            -command["p"],
            -float(command.get("raw") or command["p"]),
            str(command.get("description") or ""),
        )
    open_rows = sorted(groups["app"], key=_by_chance) + sorted(groups["explore"], key=_by_chance)
    if open_rows:
        return open_rows[:limit]
    return [dict(command) for command in plain[:limit]]


def _fully_aside(unused: dict | None, stats: dict | None, chord_id: object, now: float | None) -> bool:
    return unused_penalty(unused, chord_id, now) >= 1 or recent_use_penalty(stats, chord_id) >= 1


def available_commands(
    commands: Iterable[dict],
    unused: dict | None = None,
    now: float | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """Commands that are not fully aside. If that is everyone, the original list stays."""
    fresh = [
        command for command in commands
        if isinstance(command, dict) and not _fully_aside(unused, stats, command.get("chord_id"), now)
    ]
    return fresh or [command for command in commands if isinstance(command, dict)]


def suggest_rows(
    commands: list[dict],
    probabilities: dict,
    confidence: object,
    unused: dict | None = None,
    now: float | None = None,
    stats: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Card rows, plus every positive rank so a later card can rotate without a new call."""
    ranked = rank(commands, probabilities, confidence, limit=len(commands) or SUGGEST_COUNT)
    return suggestion_rows(offer_rows(ranked, unused, now=now, stats=stats)), ranked


def _saved_probability(probabilities: dict, command: dict) -> float:
    raw = probabilities.get(str(command.get("id") or command.get("arg") or ""))
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if number <= 0 or number > 1:
        return 0.0
    return number


def order_saved(
    commands: Iterable[dict],
    probabilities: dict,
    names: Iterable[str],
    explore: dict | None = None,
) -> list[dict]:
    """Each remembered name is its own pool, scaled to 100%, then the pools are merged.

    A shortcut in several pools keeps its best share. Equal shares both stay.
    The exploration pool is its own 100% and always leaves out those matches.
    It sits behind them, so the card leads with the apps and a rested app
    shortcut can reveal an exploration one. Without that pool, backfill from
    the same map is used only when fewer than five shortcuts match.
    A zero stays off. The confidence gate is not applied.
    """
    if not isinstance(probabilities, dict):
        return []
    outside = explore if isinstance(explore, dict) else None
    remembered = [name for name in names if isinstance(name, str) and name.strip()]
    by_name: dict[str, list[tuple[float, dict]]] = {}
    rest: list[dict] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        hits = [name for name in remembered if matches_used(command, [name])]
        if hits:
            probability = _saved_probability(probabilities, command)
            if probability <= 0:
                continue
            item = dict(command)
            item["raw"] = probability
            for name in hits:
                by_name.setdefault(name, []).append((probability, item))
            continue
        source = probabilities if outside is None else outside
        probability = _saved_probability(source, command)
        if probability <= 0:
            continue
        item = dict(command)
        item["raw"] = probability
        item["p"] = probability
        item["pool"] = "explore"
        rest.append(item)
    best: dict[str, dict] = {}
    for members in by_name.values():
        total = sum(probability for probability, _item in members)
        if total <= 0:
            continue
        for probability, item in members:
            share = probability / total
            key = str(item.get("id") or item.get("arg") or "")
            current = best.get(key)
            if current is not None and (share, probability) <= (current["p"], current["raw"]):
                continue
            chosen = dict(item)
            chosen["p"] = share
            chosen["pool"] = "app"
            best[key] = chosen
    matched = sorted(best.values(), key=lambda command: (-command["p"], -command["raw"], str(command.get("description") or "")))
    rest.sort(key=lambda command: (-command["p"], str(command.get("description") or "")))
    if outside is None and len(matched) >= SUGGEST_COUNT:
        return matched
    return matched + rest


def slice_rows(
    commands: Iterable[dict],
    probabilities: dict,
    names: Iterable[str],
    unused: dict | None = None,
    explore: dict | None = None,
    now: float | None = None,
    stats: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Five card rows from the saved pools, plus the list a later visit can rotate."""
    pool = order_saved(commands, probabilities, names, explore)
    return suggestion_rows(offer_rows(pool, unused, now=now, stats=stats)), pool


def _chord_index(commands: Iterable[dict]) -> dict[tuple[str, str], str]:
    live: dict[tuple[str, str], str] = {}
    for command in commands:
        if not isinstance(command, dict):
            continue
        ident = str(command.get("chord_id") or "")
        if not ident:
            continue
        live.setdefault((str(command.get("chord") or ""), str(command.get("description") or "")), ident)
    return live


def row_chord_ids(rows: Iterable[dict], commands: Iterable[dict]) -> list[str]:
    """Chord ids for the rows actually painted. A row Hyprland no longer has is skipped."""
    live = _chord_index(commands)
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = live.get((str(row.get("chord") or ""), str(row.get("description") or "")), "")
        if ident:
            ids.append(ident)
    return ids


def rotate_saved(
    items: Iterable[dict],
    commands: Iterable[dict],
    unused: dict | None = None,
    state: dict | None = None,
    stats: dict | None = None,
    limit: int = SUGGEST_COUNT,
    now: float | None = None,
) -> list[dict]:
    """A saved card with no ranked pool. Fully aside rows yield to other shortcuts.

    If that would leave the card empty, the original rows stay.
    """
    live = _chord_index(commands)
    originals: list[dict] = []
    ranked: list[tuple[float, int, dict]] = []
    for index, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        originals.append(row)
        key = (str(row.get("chord") or ""), str(row.get("description") or ""))
        ident = live.get(key, "")
        penalty = 0.0
        if ident and _fully_aside(unused, stats, ident, now):
            continue
        if ident:
            ignored = unused_penalty(unused, ident, now)
            used = recent_use_penalty(stats, ident)
            penalty = 1 - (1 - ignored) * (1 - used)
        ranked.append((penalty, index, row))
    ranked.sort()
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _penalty, _index, row in ranked:
        key = (str(row.get("chord") or ""), str(row.get("description") or ""))
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
        if len(kept) >= limit:
            return kept
    free = [
        command for command in commands
        if isinstance(command, dict) and not _fully_aside(unused, stats, command.get("chord_id"), now)
    ]
    for row in discovery_rows(free, state or {}, stats, limit):
        key = (str(row.get("chord") or ""), str(row.get("description") or ""))
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept or originals[:limit]
