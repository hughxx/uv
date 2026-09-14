"""Rolling decisions with provisional run updates committed after response validation."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from .actions import ActionCompiler
from .planning import Candidate, Plan, PlanArbiter
from .playbooks import Context, PlaybookLibrary, default_library
from .tasks import TaskState, after_selection, prepare_task
from .world import RuleProfile, World


@dataclass(frozen=True)
class PlaybookRun:
    candidate: Candidate
    started: int
    updated: int


@dataclass(frozen=True)
class Transition:
    key: str
    kind: str
    previous_stage: str
    next_stage: str


@dataclass(frozen=True)
class Decision:
    response: dict[str, Any]
    world: World
    rules: RuleProfile
    plan: Plan
    runs: tuple[PlaybookRun, ...]
    transitions: tuple[Transition, ...]
    diagnostics: tuple[str, ...]
    candidate_count: int
    observed_gold_delta: int | None
    task_state: TaskState | None = None


def objective_observed(run: PlaybookRun, world: World) -> bool:
    for action in run.candidate.actions:
        if action.kind == "sell":
            actor = next((unit for unit in world.actors if unit.id == action.actor_id), None)
            return actor is not None and actor.backpack is not None and action.name not in actor.backpack
        if action.kind == "build":
            return any(unit.alive and unit.kind == action.name and unit.pos in action.targets for unit in world.our)
        if action.kind == "acceptTask":
            return bool(world.phase_task)
        if action.kind == "use" and action.name.endswith(("Voucher1", "Voucher2")):
            return any(unit.alive and unit.pos in action.targets and unit.level == int(action.name[-1]) + 1 for unit in world.our)
    return False


class StrategyEngine:
    def __init__(self, *, library: PlaybookLibrary | None = None, rules: RuleProfile | None = None, budget_seconds: float = 2.5) -> None:
        self.library = library if library is not None else default_library()
        self.rules = rules if rules is not None else RuleProfile()
        self.budget_seconds = budget_seconds
        self.runs: tuple[PlaybookRun, ...] = ()
        self.last_decision: Decision | None = None
        self.task_state: TaskState | None = None

    def propose(self, world: World, *, reset: bool = False) -> Decision:
        deadline = time.monotonic() + self.budget_seconds
        rules = self.rules
        if (reset or self.last_decision is None) and world.observation.round_no == 0:
            rules = replace(rules, round_origin=0)
        compiler = ActionCompiler(rules)
        context = Context(world, rules, deadline - min(0.3, self.budget_seconds / 3))
        candidates, diagnostics = self.library.propose(context)
        task_work = prepare_task(world, None if reset else self.task_state)
        if task_work.candidate is not None:
            candidates += (task_work.candidate,)
        old = {} if reset else {run.candidate.key: run for run in self.runs}
        previous = {actor: key for key, run in old.items() for actor in run.candidate.actors}
        plan = PlanArbiter(compiler).choose(world, candidates, previous=previous, deadline=deadline)
        response = compiler.compile(world, plan.actions, prompt=task_work.prompt, execute_cmd=task_work.execute_cmd)
        selected = {candidate.key: candidate for candidate in plan.candidates}
        offered = {candidate.key for candidate in candidates}
        runs, transitions = [], []
        for key, candidate in selected.items():
            before = old.get(key)
            runs.append(PlaybookRun(candidate, before.started if before else world.observation.round_no, world.observation.round_no))
            if before is None:
                transitions.append(Transition(key, "started", "", candidate.stage))
            elif before.candidate.stage != candidate.stage or before.candidate.actors != candidate.actors:
                transitions.append(Transition(key, "progressed", before.candidate.stage, candidate.stage))
        for key, run in old.items():
            if key not in selected:
                reason = "objective-observed" if objective_observed(run, world) else "replaced" if key in offered else "premise-invalidated"
                transitions.append(Transition(key, reason, run.candidate.stage, ""))
        last_world = self.last_decision.world if self.last_decision and not reset else None
        delta = world.gold - last_world.gold if last_world and last_world.gold is not None and world.gold is not None else None
        return Decision(response, world, rules, plan, tuple(runs), tuple(transitions), diagnostics, len(candidates), delta,
                        after_selection(task_work, plan.actions, world.observation.round_no))

    def commit(self, decision: Decision) -> None:
        self.rules, self.runs, self.last_decision = decision.rules, decision.runs, decision
        self.task_state = decision.task_state
