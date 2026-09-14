"""Explicit short-horizon threat envelopes, not a replica of the judge AI."""

from __future__ import annotations

from .actions import Action
from .world import Pos, Unit, World


ROBOT_DAMAGE = {"smallRobot": 5, "middleRobot": 10, "largeRobot": 20, "bossRobot": 40}


def full_health(unit: Unit, *, level: int | None = None) -> int | None:
    if unit.kind in {"worker", "pioneer"}:
        return 220 if unit.kind == "worker" else 200
    current_level = level if level is not None else unit.level
    if current_level not in (1, 2, 3):
        return None
    if unit.kind == "station":
        return current_level * 1500
    if unit.kind in {"wall", "gatling", "railgun", "rocket"}:
        return 500 + current_level * 500
    return None


class ThreatEnvelope:
    """One attack per visible hostile robot, allowing one step before attack.

    Target selection, cadence and ordering are unknown. This is a configurable
    precaution, not an upper bound proven for the official simulator. Friendly
    kills and walls do not erase incoming damage in the same turn.
    """

    def __init__(self, world: World, *, robot_steps: int = 1) -> None:
        self.world, self.robot_steps = world, robot_steps
        self.cache: dict[Pos, int] = {}

    def damage_at(self, pos: Pos) -> int:
        if pos not in self.cache:
            damage = 0
            for robot in self.world.robots:
                if not robot.alive or robot.target_team not in {None, self.world.side} or robot.abnormal_state == "dizzy":
                    continue
                attack_range = robot.attack_range if robot.attack_range is not None and robot.attack_range >= 0 else 3
                if pos.distance(robot.pos) <= attack_range + self.robot_steps:
                    damage += ROBOT_DAMAGE.get(robot.kind, 40)
            self.cache[pos] = damage
        return self.cache[pos]

    def projected_losses(self, actions: tuple[Action, ...]) -> int:
        by_actor = {action.actor_id: action for action in actions}
        losses = 0
        for actor in self.world.actors:
            action = by_actor.get(actor.id)
            position, health = actor.pos, actor.health
            if action is not None and action.kind == "move" and len(action.targets) == 1:
                position = action.targets[0]
            elif action is not None and action.kind == "use" and action.name == "Medicine" and not action.targets:
                health = full_health(actor)
            losses += int(self.damage_at(position) >= health)
        return losses
