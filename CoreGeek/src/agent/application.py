"""Application boundary for the future stateful decision pipeline."""

from __future__ import annotations

from typing import Any

from .memory import TurnMemory
from .protocol import Observation, idle_response


class AgentApplication:
    def __init__(self) -> None:
        self.memory = TurnMemory()

    def handle_turn(self, payload: Any) -> dict[str, Any]:
        observation = Observation.from_payload(payload)
        status, cached = self.memory.lookup(observation)
        if cached is not None:
            return cached
        if status in {"stale_session", "conflicting_or_stale"}:
            return idle_response()
        response = self.decide(observation)
        self.memory.commit(observation, response)
        return response

    def decide(self, observation: Observation) -> dict[str, Any]:
        # Decision policies are added separately from transport and receipts.
        return idle_response()
