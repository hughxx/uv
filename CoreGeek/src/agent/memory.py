"""Transactional turn receipts and evidence deltas, with explicit reset boundaries."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .protocol import Observation, encode_response


@dataclass(frozen=True)
class Event:
    kind: str
    round_no: int
    subject: str = ""


def fingerprint(observation: Observation) -> str:
    content = json.dumps(observation.raw, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def identity(observation: Observation) -> tuple[str, str, str]:
    team = observation.raw.get("teamOur", {})
    if not isinstance(team, dict):
        return "", "", ""
    bases = [(unit.get("id"), unit.get("pos")) for unit in team.get("roles", [])
             if isinstance(unit, dict) and unit.get("roleType") == "station"]
    return str(team.get("teamId", "")), str(team.get("type", "")), json.dumps(bases, sort_keys=True)


class TurnMemory:
    def __init__(self) -> None:
        self.session: tuple[str, str, str] | None = None
        self.retired_sessions: set[tuple[str, str, str]] = set()
        self.last: Observation | None = None
        self.receipts: OrderedDict[tuple[int, str], bytes] = OrderedDict()
        self.news: list[tuple[str, str]] = []
        self.events: tuple[Event, ...] = ()
        self.revision = 0

    def lookup(self, observation: Observation) -> tuple[str, dict[str, Any] | None]:
        session = identity(observation)
        if session in self.retired_sessions:
            return "stale_session", None
        if self.session is not None and session != self.session:
            # Changing side/team/base identity is explicit evidence; round
            # rollback alone is not evidence that a new match has started.
            return "new_session", None
        key = observation.round_no, fingerprint(observation)
        if key in self.receipts:
            return "cached", json.loads(self.receipts[key])
        if self.last is not None and observation.round_no <= self.last.round_no:
            return "conflicting_or_stale", None
        return "new", None

    def changes(self, observation: Observation, *, reset: bool = False) -> tuple[Event, ...]:
        old = None if reset else self.last
        events = [Event("tick", observation.round_no)]
        fields = {"teamOur": "own_state", "teamEnemy": "enemy_state", "robot": "robots", "mapInfo": "map",
                  "phaseTask": "task", "llmResp": "llm_result", "lastCmdResult": "command_result", "worldNews": "news",
                  "lastRoundRoleActionResults": "action_results", "errors": "errors", "lastSummonTreasureResult": "treasure_result"}
        for field, kind in fields.items():
            if old is None or old.raw.get(field) != observation.raw.get(field):
                events.append(Event(kind, observation.round_no))
        return tuple(events)

    def commit(self, observation: Observation, response: dict[str, Any]) -> None:
        encoded = encode_response(response)
        frozen = Observation.from_payload(json.loads(json.dumps(observation.raw, allow_nan=False)))
        session = identity(frozen)
        reset = self.session is not None and session != self.session
        events = self.changes(frozen, reset=reset)
        if reset:
            self.retired_sessions.add(self.session)
            self.receipts.clear()
            self.news.clear()
        self.session = session
        self.last = frozen
        self.events = events
        self.revision += 1
        key = frozen.round_no, fingerprint(frozen)
        self.receipts[key] = encoded
        while len(self.receipts) > 32:
            self.receipts.popitem(last=False)
        news = frozen.raw.get("worldNews", {})
        if isinstance(news, dict):
            for field in ("officialNews", "folkLegends"):
                value = news.get(field)
                if isinstance(value, str) and value and (field, value) not in self.news:
                    self.news.append((field, value))
            self.news[:] = self.news[-256:]
