"""Team-wide strategic constraints, independent of individual playbooks."""

from __future__ import annotations

from .actions import Action
from .world import Pos, RuleProfile, World


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
