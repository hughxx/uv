"""Minimal protocol boundary; retain the entire observation for future planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class InvalidObservation(ValueError):
    """The request cannot be treated as a game observation."""


@dataclass(frozen=True)
class Observation:
    round_no: int
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: Any) -> Observation:
        if not isinstance(payload, dict):
            raise InvalidObservation("expected an object")
        round_no = payload.get("roundNo")
        if type(round_no) is not int or round_no < 0:
            raise InvalidObservation("expected a non-negative integer roundNo")
        return cls(round_no=round_no, raw=payload)


def idle_response() -> dict[str, Any]:
    """A complete, fresh response with no actions or tool requests."""
    return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


def encode_response(payload: Any) -> bytes:
    """Validate the envelope; action-level legality belongs to the future compiler."""
    if not isinstance(payload, dict) or not isinstance(payload.get("roleCommandMap"), dict):
        raise ValueError("response requires a roleCommandMap object")
    if not isinstance(payload.get("prompt"), str) or not isinstance(payload.get("executeCmd"), str):
        raise ValueError("response requires string tool fields")
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
