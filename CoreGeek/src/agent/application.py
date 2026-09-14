"""Application boundary for the future stateful decision pipeline."""

from __future__ import annotations

from typing import Any

from .memory import TurnMemory
from .protocol import Observation, idle_response
from .strategy import Decision, StrategyEngine
from .world import World


class AgentApplication:
    def __init__(self, engine: StrategyEngine | None = None) -> None:
        self.memory = TurnMemory()
        self.engine = engine if engine is not None else StrategyEngine()
        self._pending: Decision | None = None
        self._reset = False

    def handle_turn(self, payload: Any) -> dict[str, Any]:
        observation = Observation.from_payload(payload)
        status, cached = self.memory.lookup(observation)
        if cached is not None:
            return cached
        if status in {"stale_session", "conflicting_or_stale"}:
            return idle_response()
        self._pending = None
        self._reset = status == "new_session"
        response = self.decide(observation)
        self.memory.commit(observation, response)
        if self._pending is not None:
            self.engine.commit(self._pending)
        return response

    def decide(self, observation: Observation) -> dict[str, Any]:
        # Minimal requests remain useful for transport probes; partial game
        # observations are never filled with invented entities or money.
        if "mapInfo" not in observation.raw or "teamOur" not in observation.raw:
            return idle_response()
        world = World.from_observation(observation)
        self._pending = self.engine.propose(world, reset=self._reset)
        return self._pending.response
