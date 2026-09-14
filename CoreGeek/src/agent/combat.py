"""Area-consumable opportunities evaluated jointly with weapon fire."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .actions import Action
from .forecast import ROBOT_DAMAGE
from .planning import Candidate, Value

if TYPE_CHECKING:
    from .playbooks import Context


class AreaConsumables:
    id = "area-consumables"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        world = context.world
        if context.rules.daytime(world.observation.round_no):
            return ()
        if not any({"Bomb", "DizzyWeapon"} & set(actor.backpack or ()) for actor in world.actors):
            return ()
        hostile = tuple(robot for robot in world.robots if robot.alive and robot.target_team in {None, world.side})
        if not hostile:
            return ()
        # Unlike weapon fire, the rules explicitly allow map-wide item centers,
        # including empty cells. Deduplicate equivalent coverage sets.
        centers = sorted({pos for robot in hostile for pos in (robot.pos, *robot.pos.neighbors()) if world.inside(pos)})
        coverage = {}
        for center in centers:
            if context.expired:
                break
            hits = tuple(robot for robot in hostile if center.distance(robot.pos) <= 1)
            # Equal friendly coverage can have different opponent collateral.
            key = tuple(robot.id for robot in world.robots if robot.alive and center.distance(robot.pos) <= 1)
            if hits and key not in coverage:
                coverage[key] = center, hits
        proposals = []
        for actor in world.actors:
            for item in ("Bomb", "DizzyWeapon"):
                if item not in (actor.backpack or ()):
                    continue
                for center, hits in coverage.values():
                    if context.expired:
                        break
                    # Owned inventory has opportunity cost even though using it
                    # does not spend current gold. The fallback price is official.
                    item_cost = world.prices.get(item, 100) * 0.2
                    if item == "Bomb":
                        # Items affect BOTH waves. Penalize help accidentally
                        # given to the opponent, whose kill credit is unknown.
                        helped_enemy = sum(min(100, robot.health) for robot in world.robots if robot.alive
                                           and robot.target_team not in {None, world.side} and center.distance(robot.pos) <= 1)
                        damage = tuple((robot.id, min(100, robot.health)) for robot in hits)
                        readiness = -item_cost - helped_enemy * 0.2
                        resources = frozenset()
                    else:
                        active = tuple(robot for robot in hits if robot.abnormal_state != "dizzy")
                        if not active:
                            continue
                        # Discounted three-turn prevented pressure, not a claim
                        # that every stunned robot would otherwise attack us.
                        pressure = sum(ROBOT_DAMAGE.get(robot.kind, 40) for robot in active
                                       if world.station and min(robot.pos.distance(cell) for cell in world.station.cells) <= 6)
                        helped_enemy = sum(ROBOT_DAMAGE.get(robot.kind, 40) for robot in world.robots if robot.alive
                                           and robot.target_team not in {None, world.side} and robot.abnormal_state != "dizzy"
                                           and center.distance(robot.pos) <= 1)
                        damage, readiness = (), pressure * 0.6 - item_cost - helped_enemy * 0.6
                        resources = frozenset(f"stun:{robot.id}" for robot in active)
                    proposals.append(Candidate(f"area:{actor.id}:{item}", self.id, frozenset({actor.id}),
                                              (Action(actor.id, "use", (center,), item),), Value(readiness=readiness),
                                              "use-area-item", ("item-owned", "observed-3x3-coverage", "inventory-opportunity-cost", "effect-order-uncalibrated"),
                                              resources, damage))
        return tuple(proposals)
