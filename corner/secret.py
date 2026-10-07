"""The Jev key lives in the Secret Service, not in a file or the environment.

Other Omarchy plugins use the same tool: /usr/bin/secret-tool, with the
secret on stdin. The process list never sees it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SECRET_TOOL = "/usr/bin/secret-tool"
SERVICE = "ignotas.mouse-sacrifice"
ITEM = "jev"
LABEL = "Jev API key"


def lookup(tool: str = SECRET_TOOL) -> str:
    """The stored key, or "" when the keyring has none."""
    completed = _run(tool, ["lookup", "service", SERVICE, "key", ITEM])
    if completed is None or completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def store(secret: str, tool: str = SECRET_TOOL) -> bool:
    """Save one key. A blank or multi-line value is refused."""
    secret = secret.strip()
    if not secret or any(char in secret for char in "\n\r\x00"):
        return False
    completed = _run(
        tool,
        ["store", "--label", LABEL, "service", SERVICE, "key", ITEM],
        secret,
    )
    return completed is not None and completed.returncode == 0


def clear(tool: str = SECRET_TOOL) -> bool:
    completed = _run(tool, ["clear", "service", SERVICE, "key", ITEM])
    return completed is not None and completed.returncode == 0


def read_key(legacy_file: Path | None = None, tool: str = SECRET_TOOL) -> str:
    """Keyring first. A leftover plaintext file is moved in, then deleted."""
    current = lookup(tool)
    if current:
        _remove_file(legacy_file)
        return current
    if legacy_file is None or not legacy_file.is_file():
        return ""
    try:
        secret = legacy_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not store(secret, tool):
        return ""
    _remove_file(legacy_file)
    return secret.strip()


def _remove_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        return


def _run(tool: str, args: list[str], text: str = "") -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [tool, *args],
            input=text,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
