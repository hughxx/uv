"""Team-level candidate arbitration; scores never bypass resource legality."""

from __future__ import annotations

import time
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .actions import Action, ActionCompiler, InvalidAction, structured_action
from .forecast import ThreatEnvelope
from .layout import LayoutGuard
from .strategy_policy import DefensivePostPolicy
from .world import Unit, World


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
    projected_role_losses: int = 0
    threatened_posts_lost: int = 0
    rejections: tuple[tuple[str, int], ...] = ()
    empty_reason: str = ""

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(action for candidate in self.candidates for action in candidate.actions)


def valid_candidate(candidate: object) -> bool:
    if not isinstance(candidate, Candidate) or not isinstance(candidate.value, Value):
        return False
    numeric = (candidate.value.score, candidate.value.gold, candidate.value.readiness,
               candidate.value.risk, candidate.value.occupied_turns)
    return (isinstance(candidate.key, str) and bool(candidate.key)
            and isinstance(candidate.actors, frozenset) and bool(candidate.actors)
            and all(isinstance(actor, str) for actor in candidate.actors)
            and isinstance(candidate.resources, frozenset)
            and all(isinstance(resource, str) for resource in candidate.resources)
            and all(type(value) in (float, int) and math.isfinite(value) for value in numeric)
            and type(candidate.reserved_gold) is int and candidate.reserved_gold >= 0
            and type(candidate.reserved_weapon_slots) is int and candidate.reserved_weapon_slots >= 0
            and (candidate.deadline is None or type(candidate.deadline) is int)
            and isinstance(candidate.actions, tuple)
            and all(structured_action(action) and action.actor_id in candidate.actors for action in candidate.actions)
            and isinstance(candidate.damage, tuple)
            and all(isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str)
                    and type(pair[1]) in (int, float) and math.isfinite(pair[1]) and pair[1] >= 0
                    for pair in candidate.damage))


def gold_commitment(world: World, candidate: Candidate, compiler: ActionCompiler) -> int:
    immediate = sum(world.prices.get(action.name, 0) * action.amount if action.kind == "buy"
                    else compiler.rules.weapon_cost if action.kind == "build" and action.name != "wall" else 0
                    for action in candidate.actions)
    # reserved_gold covers the remaining plan INCLUDING its current action.
    return max(candidate.reserved_gold, immediate)


def combat_utility(world: World, candidates: tuple[Candidate, ...], *, robots: Mapping[str, Unit] | None = None) -> float:
    combined: dict[str, float] = {}
    for candidate in candidates:
        for robot_id, damage in candidate.damage:
            combined[robot_id] = combined.get(robot_id, 0) + damage
    score = 0.0
    if not combined:
        return score
    robots = robots if robots is not None else {robot.id: robot for robot in world.robots}
    station = world.station
    rewards = {"smallRobot": 1, "middleRobot": 2, "largeRobot": 4, "bossRobot": 10}
    for robot_id, predicted_damage in combined.items():
        robot = robots.get(robot_id)
        if robot is None or not robot.alive:
            continue
        damage = min(robot.health, predicted_damage)
        threat = 1.0
        if station and min(robot.pos.distance(cell) for cell in station.cells) <= 5:
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
        robots = {robot.id: robot for robot in world.robots}
        threat = ThreatEnvelope(world)
        layout = LayoutGuard(world)
        posts = DefensivePostPolicy(world, self.compiler.rules)
        guard = self.compiler.rules.guard_projected_role_deaths
        losses = lambda actions: threat.projected_losses(actions) if guard else 0
        valid = tuple(candidate for candidate in candidates if valid_candidate(candidate) and candidate.actors <= actor_ids
                      and (candidate.deadline is None or candidate.deadline >= world.observation.round_no))
        rejections: Counter[str] = Counter()
        if len(valid) != len(candidates):
            rejections["invalid_or_expired_candidate"] = len(candidates) - len(valid)
        options: dict[str, list[Candidate]] = {}
        for actor_id in actor_ids:
            ranked = sorted((candidate for candidate in valid if actor_id in candidate.actors),
                            key=lambda c: (losses(c.actions), posts.lost_posts(c.actions),
                                           -(c.value.utility + combat_utility(world, (c,), robots=robots)), c.key))
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
        best_losses = losses(())
        best_posts_lost = 0
        feasible_actions = 0

        def search(remaining: frozenset[str], chosen: tuple[Candidate, ...], resources: frozenset[str]) -> None:
            nonlocal best, best_value, visited, exhausted, best_losses, best_posts_lost, feasible_actions
            if visited >= self.max_nodes or time.monotonic() >= deadline:
                exhausted = True
                return
            visited += 1
            if not remaining:
                actions = tuple(action for candidate in chosen for action in candidate.actions)
                reserved_gold = sum(gold_commitment(world, candidate, self.compiler) for candidate in chosen)
                if reserved_gold and (world.gold is None or reserved_gold > world.gold):
                    rejections["shared_gold"] += 1
                    return
                weapon_slots = sum(max(candidate.reserved_weapon_slots,
                                       sum(action.kind == "build" and action.name != "wall" for action in candidate.actions))
                                   for candidate in chosen)
                if weapon_slots and weapon_slots + len(world.weapons) > self.compiler.rules.max_weapons:
                    rejections["weapon_slots"] += 1
                    return
                try:
                    self.compiler.validate(world, actions)
                except InvalidAction as error:
                    frame = error.__traceback__
                    while frame is not None and frame.tb_next is not None:
                        frame = frame.tb_next
                    site = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}" if frame else "unknown"
                    rejections[f"action_validation@{site}"] += 1
                    return
                if self.compiler.rules.preserve_build_access and not layout.preserves_access(actions, deadline):
                    rejections["layout_or_deadline"] += 1
                    return
                feasible_actions += int(bool(actions))
                switching = sum(0.2 for candidate in chosen for actor in candidate.actors if actor in previous and previous[actor] != candidate.key)
                utility = sum(candidate.value.utility for candidate in chosen) + combat_utility(world, chosen, robots=robots) - switching
                projected = losses(actions)
                posts_lost = posts.lost_posts(actions)
                if (projected, posts_lost, -utility) < (best_losses, best_posts_lost, -best_value):
                    best, best_value, best_losses, best_posts_lost = chosen, utility, projected, posts_lost
                elif actions:
                    rejections["not_preferred_by_policy_or_utility"] += 1
                return
            actor_id = min(remaining)
            for candidate in options[actor_id]:
                if candidate.actors <= remaining and not candidate.resources & resources:
                    search(remaining - candidate.actors, chosen + (candidate,), resources | candidate.resources)
            search(remaining - {actor_id}, chosen, resources)

        search(actor_ids, (), frozenset())
        empty = ""
        if not any(candidate.actions for candidate in best):
            empty = ("selected_hold" if best else "no_living_actors" if not actor_ids else "search_budget" if exhausted and not feasible_actions
                     else "no_candidates" if not candidates else "no_feasible_action" if not feasible_actions else "wait_preferred")
        return Plan(best, best_value, visited, exhausted, best_losses, best_posts_lost, tuple(sorted(rejections.items())), empty)
