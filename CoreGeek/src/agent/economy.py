"""Observed-price investment proposals, including procurement and delivery cost."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .actions import Action
from .forecast import full_health
from .planning import Candidate, Value
from .world import Unit

if TYPE_CHECKING:
    from .playbooks import Context


def upgrade_item(building: Unit) -> str | None:
    prefix = ("Station" if building.kind == "station" else "Wall" if building.kind == "wall"
              else "Weapon" if building.kind in {"gatling", "railgun", "rocket"} else None)
    return f"{prefix}UpgradeVoucher{building.level}" if prefix and building.level in (1, 2) else None


def upgrade_value(building: Unit) -> float:
    """Provisional asset value, NOT score or a calibrated return on investment."""
    maximum = full_health(building)
    missing = max(0, maximum - building.health) if maximum else 0
    if building.kind == "station":
        return 30 + missing / 50
    if building.kind == "wall":
        return 8 + missing / 100
    return 40 + (10 if building.level == 2 else 0) + missing / 100


class InvestUpgrades:
    id = "invest-upgrades"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        from .playbooks import approach_or_act

        world = context.world
        if world.gold is None or not context.rules.daytime(world.observation.round_no):
            return ()
        # Existing vouchers are private, but visible ownership prevents buying
        # a second copy while the first still needs delivery. No item transfer.
        owned = {item for actor in world.actors for item in actor.backpack or ()}
        proposals = []
        for actor in world.actors:
            if actor.kind != "worker" or actor.free_slots is None or actor.free_slots < 1:
                continue
            for building in world.our:
                item = upgrade_item(building) if building.alive else None
                if context.expired:
                    break
                if item is None or item in owned or item not in world.prices or world.prices[item] > world.gold:
                    continue
                step = approach_or_act(context, actor, frozenset(world.neutral("weaponShop")), Action(actor.id, "buy", name=item))
                if step is None:
                    continue
                action, purchase_eta = step
                stand = context.route(actor, frozenset(world.neutral("weaponShop"))).cells[-1]
                delivery = context.route_from(actor, stand, building.cells)
                if delivery is None:
                    continue
                eta = purchase_eta + delivery.distance + 1
                if eta > min(context.horizon, context.rules.daylight_left(world.observation.round_no)):
                    continue
                proposals.append(Candidate(f"invest:{building.id}", self.id, frozenset({actor.id}), (action,),
                                          Value(gold=-world.prices[item], readiness=upgrade_value(building),
                                                occupied_turns=eta, risk=context.risk(stand)),
                                          "buy-voucher" if action.kind == "buy" else "approach-shop",
                                          ("shop-price-observed", "full-delivery-route", "asset-value-uncalibrated"),
                                          frozenset({f"upgrade:{building.id}", f"procure:{item}"}),
                                          reserved_gold=world.prices[item]))
        return tuple(proposals)


class RestockMedicine:
    id = "restock-medicine"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        from .playbooks import approach_or_act

        world = context.world
        price = world.prices.get("Medicine")
        if price is None or world.gold is None or price > world.gold:
            return ()
        proposals = []
        for actor in world.actors:
            maximum = full_health(actor)
            if actor.health >= maximum / 2 or "Medicine" in (actor.backpack or ()) or actor.free_slots is None or actor.free_slots < 1:
                continue
            step = approach_or_act(context, actor, frozenset(world.neutral("weaponShop")), Action(actor.id, "buy", name="Medicine"))
            if step:
                action, eta = step
                if eta + 1 > context.horizon:
                    continue
                proposals.append(Candidate(f"medicine:{actor.id}", self.id, frozenset({actor.id}), (action,),
                                          Value(gold=-price, readiness=(maximum - actor.health) / 10, occupied_turns=eta + 1,
                                                risk=context.risk(actor.pos)),
                                          "buy-medicine" if action.kind == "buy" else "approach-shop", ("low-health", "shop-price-observed"),
                                          reserved_gold=price))
        return tuple(proposals)
