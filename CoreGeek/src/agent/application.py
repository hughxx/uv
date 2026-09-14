"""Application boundary for the future stateful decision pipeline."""

from __future__ import annotations

from typing import Any

from .protocol import Observation, idle_response


class AgentApplication:
    def handle_turn(self, payload: Any) -> dict[str, Any]:
        observation = Observation.from_payload(payload)
        return self.decide(observation)

    def decide(self, observation: Observation) -> dict[str, Any]:
        # Packaging baseline only. No strategy or persistent match state yet.
        return idle_response()
