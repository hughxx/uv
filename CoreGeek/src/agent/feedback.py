"""Short, reversible cooldowns after repeated confirmed execution failures."""

from __future__ import annotations

from dataclasses import dataclass

from .actions import Action
from .planning import Plan
from .world import Pos, World


@dataclass(frozen=True)
class FailedAttempt:
    actor: str
    kind: str
    targets: tuple[Pos, ...]
    item: str
    streak: int
    observed_round: int
    retry_after: int

    def matches(self, action: Action) -> bool:
        return (self.actor, self.kind, self.targets, self.item) == (action.actor_id, action.kind, action.targets, action.name)


def reconcile_failures(world: World, previous_world: World | None, previous_plan: Plan | None,
                       previous: tuple[FailedAttempt, ...]) -> tuple[FailedAttempt, ...]:
    current = world.observation.round_no
    failures = [failure for failure in previous if current - failure.observed_round < 12]
    if previous_world is None or previous_plan is None:
        return ()
    # A changed map can replenish a vein or reopen a path; old failure evidence
    # must not permanently forbid the refreshed opportunity.
    if world.observation.raw.get("mapInfo") != previous_world.observation.raw.get("mapInfo"):
        failures = []
    if current != previous_world.observation.round_no + 1:
        return tuple(failures)
    results = world.observation.raw.get("lastRoundRoleActionResults", {})
    if not isinstance(results, dict):
        return tuple(failures)
    for action in previous_plan.actions:
        if action.kind not in {"move", "collect", "build", "buy", "sell"}:
            continue
        old = next((failure for failure in failures if failure.matches(action)), None)
        result = results.get(action.key)
        if result is True:
            failures = [failure for failure in failures if not failure.matches(action)]
        elif result is False:
            streak = old.streak + 1 if old else 1
            failures = [failure for failure in failures if not failure.matches(action)]
            failures.append(FailedAttempt(action.actor_id, action.kind, action.targets, action.name,
                                          streak, current, current + 3 if streak >= 2 else current))
    return tuple(failures[-128:])


def cooling_down(action: Action, failures: tuple[FailedAttempt, ...], current: int) -> bool:
    return any(failure.retry_after > current and failure.matches(action) for failure in failures)
