"""Team-wide strategic constraints, independent of individual playbooks."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time

from .actions import Action
from .geometry import interaction_cells
from .world import Pos, RuleProfile, World


@dataclass(frozen=True)
class ReturnCommitment:
    actor_id: str
    pos: Pos
    turns: int  # Includes this action and arrival/transaction, not just travel.


@dataclass(frozen=True)
class ReturnCheck:
    status: str = "not-applicable"
    required: int = 0
    available_moves: int = 0
    before: tuple[int, int] | None = None
    after: tuple[int, int] | None = None


class DefenseReturnPolicy:
    """Minimize unmatched operators and overdue route steps in a joint plan.

    Daylight workers reserve two setup turns before night; free pioneers join
    at night. This is a conservative deployment policy, NOT a wave-arrival or
    win forecast. Paths ignore friendly actors to allow joint handovers; only
    the current joint step is collision-validated, not the full future route.
    """

    def __init__(self, world: World, rules: RuleProfile, *, deadline: float) -> None:
        self.active = False
        self.status = "not-applicable"
        night = not rules.daytime(world.observation.round_no)
        self.actor_ids = tuple(actor.id for actor in world.actors
                               if actor.kind == "worker" or (night and not world.phase_task))
        self.positions = {actor.id: actor.pos for actor in world.actors if actor.id in self.actor_ids}
        self.required = min(len(self.actor_ids), len(world.weapons))
        self.available_moves = max(0, rules.daylight_left(world.observation.round_no) - 1 - 2)
        self.maps: list[dict[Pos, int]] = []
        self.cache: dict[tuple, tuple[int, int]] = {}
        if not rules.timely_defense_return:
            self.status = "disabled"
            return
        if not world.station or not self.required:
            return
        if len(world.weapons) > 3 or len(self.actor_ids) > 8:
            self.status = "size-limit"
            return
        # Keep policy preparation bounded and leave search/serialization time.
        now = time.monotonic()
        cutoff = min(deadline, now + min(0.12, max(0, deadline - now) / 2))
        blocked = world.blockers(exclude_actors=frozenset(actor.id for actor in world.actors))
        for weapon in world.weapons:
            distances = {pos: 0 for pos in interaction_cells(world, weapon.cells, blocked)}
            frontier = deque(distances)
            while frontier:
                if time.monotonic() >= cutoff:
                    self.status = "budget"
                    return  # Partial fields are unknown, never zero-distance paths.
                current = frontier.popleft()
                for neighbor in current.neighbors():
                    if world.inside(neighbor) and neighbor not in blocked and neighbor not in distances:
                        distances[neighbor] = distances[current] + 1
                        frontier.append(neighbor)
            self.maps.append(distances)
        self.active, self.status = True, "ready"

    def cost(self, actions: tuple[Action, ...], commitments: tuple[ReturnCommitment, ...] = ()) -> tuple[int, int]:
        if not self.active:
            return (0, 0)
        moves = tuple(sorted((a.actor_id, a.targets[0]) for a in actions if a.kind == "move" and a.actor_id in self.positions))
        accepting = frozenset(a.actor_id for a in actions if a.kind == "acceptTask")
        key = moves, accepting, commitments
        if key in self.cache:
            return self.cache[key]
        positions = {**self.positions, **dict(moves)}
        elapsed = {}
        for commitment in commitments:
            if commitment.actor_id in positions:
                positions[commitment.actor_id] = commitment.pos
                elapsed[commitment.actor_id] = commitment.turns - 1
        # Weapon bitmask DP: each actor and weapon used at most once. Skipping
        # an actor lets surplus staff work elsewhere and permits substitutions.
        costs = {0: 0}
        for actor_id, pos in positions.items():
            if actor_id in accepting:
                continue
            following = dict(costs)
            for mask, late in costs.items():
                for index, distances in enumerate(self.maps):
                    bit = 1 << index
                    if mask & bit or pos not in distances:
                        continue
                    candidate = late + max(0, elapsed.get(actor_id, 0) + distances[pos] - self.available_moves)
                    following[mask | bit] = min(following.get(mask | bit, candidate), candidate)
            costs = following
        result = min((self.required - mask.bit_count(), late) for mask, late in costs.items())
        self.cache[key] = result
        return result

    def check(self, actions: tuple[Action, ...], commitments: tuple[ReturnCommitment, ...] = ()) -> ReturnCheck:
        return ReturnCheck(self.status, self.required, self.available_moves,
                           self.cost(()) if self.active else None, self.cost(actions, commitments) if self.active else None)


class DefensivePostPolicy:
    """Preserve staffed posts under nearby pressure, allowing joint handovers.

    This measures next-turn staffing, NOT shots fired or guaranteed protection.
    Tasks and healing may still consume an operator's current action.
    """

    def __init__(self, world: World, rules: RuleProfile) -> None:
        self.world = world
        station = world.station
        self.active = bool(rules.preserve_threatened_posts and not rules.daytime(world.observation.round_no) and station
                           and any(robot.alive and robot.target_team in {None, world.side} and robot.abnormal_state != "dizzy"
                                   and min(robot.pos.distance(cell) for cell in station.cells) <= 6 for robot in world.robots))
        self.cache: dict[tuple[tuple[str, Pos], ...], int] = {}
        self.baseline = self.staffed(()) if self.active else 0

    def staffed(self, actions: tuple[Action, ...]) -> int:
        moves = tuple(sorted((action.actor_id, action.targets[0]) for action in actions if action.kind == "move"))
        if moves in self.cache:
            return self.cache[moves]
        positions = dict(moves)
        actors = self.world.actors
        adjacent = {actor.id: tuple(weapon.id for weapon in self.world.weapons
                                   if positions.get(actor.id, actor.pos).distance(weapon.pos) == 1) for actor in actors}

        def match(index: int, occupied: frozenset[str]) -> int:
            if index == len(actors):
                return len(occupied)
            best = match(index + 1, occupied)
            for weapon in adjacent[actors[index].id]:
                if weapon not in occupied:
                    best = max(best, match(index + 1, occupied | {weapon}))
            return best

        self.cache[moves] = match(0, frozenset())
        return self.cache[moves]

    def lost_posts(self, actions: tuple[Action, ...]) -> int:
        return max(0, self.baseline - self.staffed(actions)) if self.active else 0
