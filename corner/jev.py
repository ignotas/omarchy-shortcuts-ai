"""One TypeSafe / Jev call. The model ranks each Choice; it does not write text."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TIMEOUT = 2.5


class JevError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _valid_choice(answer: object) -> bool:
    probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
    confidence = answer.get("confidence") if isinstance(answer, dict) else None
    return (
        isinstance(probabilities, dict)
        and not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0 <= float(confidence) <= 1
    )


def ranking_ok(request: dict, body: dict) -> bool:
    """Every question that was asked came back as one probability pool."""
    answers = body.get("answers") if isinstance(body, dict) else None
    questions = request.get("questions") if isinstance(request, dict) else None
    if not isinstance(answers, dict) or not isinstance(questions, dict) or not questions:
        return False
    return all(_valid_choice(answers.get(ident)) for ident in questions)


def ask(api_key: str, request: dict, timeout: float = TIMEOUT) -> dict:
    """POST a system-one request. Returns the parsed JSON body.

    The connection is a fresh TLS request. These calls are rare (a corner
    hover, then a minute of cache), and a long-lived socket is not worth
    the failure modes.
    """
    payload = json.dumps(request).encode()
    http = urllib.request.Request(
        ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(http, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:240]
        raise JevError(_public_http(exc.code, detail), exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError("Jev did not answer in time.") from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JevError("Jev sent a response that was not JSON.") from exc
    if not ranking_ok(request, body):
        raise JevError("Jev sent no ranking.")
    return body


def _public_http(status: int, detail: str) -> str:
    if status == 401:
        return "Jev refused the API key."
    if status == 429 or status == 529:
        return "Jev is busy. Try the corner again in a moment."
    if status == 422:
        return "Jev rejected the question."
    return f"Jev returned HTTP {status}."
