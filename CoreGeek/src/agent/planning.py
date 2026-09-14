"""Team-level candidate arbitration; scores never bypass resource legality."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from .actions import Action, ActionCompiler, InvalidAction
from .world import World


@dataclass(frozen=True)
class Value:
    score: float = 0.0
    gold: float = 0.0
    readiness: float = 0.0
    risk: float = 0.0
    occupied_turns: float = 0.0

    @property
    def utility(self) -> float:
        # Explicit baseline heuristics, not official score conversion or
        # calibrated win probabilities. All proposals use this same scale.
        return self.score + self.gold * 0.2 + self.readiness - self.risk - self.occupied_turns * 0.05


@dataclass(frozen=True)
class Candidate:
    key: str
    definition: str
    actors: frozenset[str]
    actions: tuple[Action, ...]
    value: Value
    stage: str
    evidence: tuple[str, ...] = ()
    resources: frozenset[str] = frozenset()
    damage: tuple[tuple[str, float], ...] = ()
    deadline: int | None = None
    reserved_gold: int = 0
    reserved_weapon_slots: int = 0


@dataclass(frozen=True)
class Plan:
    candidates: tuple[Candidate, ...] = ()
    utility: float = 0.0
    visited: int = 0
    exhausted: bool = False

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(action for candidate in self.candidates for action in candidate.actions)


def combat_utility(world: World, candidates: tuple[Candidate, ...]) -> float:
    combined: dict[str, float] = {}
    for candidate in candidates:
        for robot_id, damage in candidate.damage:
            combined[robot_id] = combined.get(robot_id, 0) + damage
    score = 0.0
    rewards = {"smallRobot": 1, "middleRobot": 2, "largeRobot": 4, "bossRobot": 10}
    for robot in world.robots:
        if not robot.alive:
            continue
        damage = min(robot.health, combined.get(robot.id, 0))
        threat = 1.0
        if world.station and robot.pos.distance(world.station.pos) <= 5:
            threat = 2.0
        score += damage * 0.2 * threat
        if damage >= robot.health:
            score += rewards.get(robot.kind, 0)
    return score


class PlanArbiter:
    def __init__(self, compiler: ActionCompiler, *, per_actor: int = 10, max_nodes: int = 5000) -> None:
        self.compiler, self.per_actor, self.max_nodes = compiler, per_actor, max_nodes

    def choose(self, world: World, candidates: tuple[Candidate, ...], *, previous: Mapping[str, str], deadline: float) -> Plan:
        actor_ids = frozenset(unit.id for unit in world.actors)
        valid = tuple(candidate for candidate in candidates if candidate.actors and candidate.actors <= actor_ids
                      and (candidate.deadline is None or candidate.deadline >= world.observation.round_no))
        options: dict[str, list[Candidate]] = {}
        for actor_id in actor_ids:
            ranked = sorted((candidate for candidate in valid if actor_id in candidate.actors),
                            key=lambda c: (-(c.value.utility + combat_utility(world, (c,))), c.key))
            # Keep goal/resource diversity before filling alternate shot choices;
            # otherwise one weapon's many targets crowd every other weapon out.
            seen: set[str] = set()
            first, alternatives = [], []
            for candidate in ranked:
                if candidate.key in seen:
                    alternatives.append(candidate)
                else:
                    seen.add(candidate.key)
                    first.append(candidate)
            options[actor_id] = (first + alternatives)[:self.per_actor]
        best: tuple[Candidate, ...] = ()
        best_value, visited, exhausted = 0.0, 0, False

        def search(remaining: frozenset[str], chosen: tuple[Candidate, ...], resources: frozenset[str]) -> None:
            nonlocal best, best_value, visited, exhausted
            if visited >= self.max_nodes or time.monotonic() >= deadline:
                exhausted = True
                return
            visited += 1
            if not remaining:
                actions = tuple(action for candidate in chosen for action in candidate.actions)
                reserved_gold = sum(candidate.reserved_gold for candidate in chosen)
                if reserved_gold and (world.gold is None or reserved_gold > world.gold):
                    return
                if sum(candidate.reserved_weapon_slots for candidate in chosen) + len(world.weapons) > self.compiler.rules.max_weapons:
                    return
                try:
                    self.compiler.validate(world, actions)
                except InvalidAction:
                    return
                switching = sum(0.2 for candidate in chosen for actor in candidate.actors if actor in previous and previous[actor] != candidate.key)
                utility = sum(candidate.value.utility for candidate in chosen) + combat_utility(world, chosen) - switching
                if utility > best_value:
                    best, best_value = chosen, utility
                return
            actor_id = min(remaining)
            for candidate in options[actor_id]:
                if candidate.actors <= remaining and not candidate.resources & resources:
                    search(remaining - candidate.actors, chosen + (candidate,), resources | candidate.resources)
            search(remaining - {actor_id}, chosen, resources)

        search(actor_ids, (), frozenset())
        return Plan(best, best_value, visited, exhausted)
