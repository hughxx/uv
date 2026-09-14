"""Cross-turn task tools. Commands are sent to the judge, never run locally."""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .actions import Action
from .planning import Candidate, Value
from .world import World

if TYPE_CHECKING:
    from .playbooks import Context


class AcquireTask:
    id = "acquire-task"

    def propose(self, context: Context) -> tuple[Candidate, ...]:
        from .playbooks import approach_or_act

        world = context.world
        if world.phase_task:
            return ()
        proposals = []
        for actor in world.actors:
            if actor.kind != "pioneer":
                continue
            for task in world.observation.raw["teamOur"].get("playerTasks", []):
                if context.expired or task.get("isValid") is not True or task.get("coldDownRounds") != 0:
                    continue
                cells = world.task_cells(task)
                step = approach_or_act(context, actor, cells, Action(actor.id, "acceptTask"))
                if not step:
                    continue
                action, eta = step
                base = task.get("scoreReward")
                gold = task.get("goldReward")
                if type(base) is not int or type(gold) is not int:
                    continue
                anchor = min(cells)
                proposals.append(Candidate(f"task:{anchor.x}:{anchor.y}", self.id, frozenset({actor.id}), (action,),
                                          Value(score=base * 0.4, gold=gold * 0.4, occupied_turns=eta + 6,
                                                risk=context.risk(context.route(actor, cells).cells[-1]) * 2),
                                          "accept" if action.kind == "acceptTask" else "approach-task", ("task-available", "solver-success-uncalibrated"),
                                          frozenset({f"task:{anchor.x}:{anchor.y}"})))
        return tuple(proposals)


@dataclass(frozen=True)
class TaskState:
    text: str
    started: int
    phase: str = "need-llm"
    step: int = 0
    sent_round: int = -1
    request_id: str = ""
    marker: str = ""
    answer: str = ""
    trace: tuple[str, ...] = ()
    command_baseline: str = ""


@dataclass(frozen=True)
class TaskWork:
    state: TaskState | None
    prompt: str = ""
    execute_cmd: str = ""
    candidate: Candidate | None = None


def append_trace(state: TaskState, text: str) -> TaskState:
    if len(text) > 10000:
        text = "[earlier output omitted; obtain targeted details if needed]\n" + text[-10000:]
    return replace(state, trace=(*state.trace, text)[-4:])


def request_token(world: World, state: TaskState) -> str:
    content = f"{world.side}:{state.started}:{state.step}:{world.observation.round_no}:{state.text}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]


def sandbox_command(command: str, marker: str) -> str:
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    program = ("import base64,subprocess; "
               f"print({marker!r},flush=True); "
               f"raise SystemExit(subprocess.call(base64.b64decode({encoded!r}).decode('utf-8'),shell=True))")
    return "python -c " + shlex.quote(program)


def parse_reply(text: str, token: str) -> dict | None:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) and value.get("requestId") == token else None


def prepare_task(world: World, previous: TaskState | None, *, max_requests: int = 12, timeout_rounds: int = 3) -> TaskWork:
    pioneer = next((actor for actor in world.actors if actor.kind == "pioneer"), None)
    if not world.phase_task or pioneer is None:
        return TaskWork(None)
    current = world.observation.round_no
    state = previous if previous is not None and previous.text == world.phase_task else TaskState(world.phase_task, current)
    if state.phase == "waiting-llm":
        reply = parse_reply(str(world.observation.raw.get("llmResp", "")), state.request_id) if current > state.sent_round else None
        if reply is not None:
            if reply.get("kind") == "answer" and isinstance(reply.get("answer"), (str, dict, list)):
                answer = reply["answer"] if isinstance(reply["answer"], str) else json.dumps(reply["answer"], ensure_ascii=False)
                if answer.strip() and len(answer) <= 64000:
                    state = replace(state, phase="answer-ready", answer=answer)
                else:
                    state = replace(append_trace(state, "Answer must be nonempty and within 64000 characters."), phase="need-llm")
            elif reply.get("kind") == "execute" and isinstance(reply.get("command"), str) and 0 < len(reply["command"]) <= 8000 and "\x00" not in reply["command"]:
                if state.step >= max_requests:
                    return TaskWork(replace(state, phase="budget-exhausted"))
                marker = "__CG_TASK_" + request_token(world, state) + "__"
                state = replace(append_trace(state, "Command requested:\n" + reply["command"]),
                                phase="waiting-command", step=state.step + 1, marker=marker, sent_round=current,
                                command_baseline=str(world.observation.raw.get("lastCmdResult", "")))
                return TaskWork(state, execute_cmd=sandbox_command(reply["command"], marker))
            else:
                state = replace(append_trace(state, "Invalid reply. Return one execute or answer JSON object with the exact requestId."), phase="need-llm")
        elif current - state.sent_round >= timeout_rounds:
            state = replace(append_trace(state, "No correlated LLM reply arrived; return the exact requestId."), phase="need-llm")
        else:
            return TaskWork(state)
    elif state.phase == "waiting-command":
        result = str(world.observation.raw.get("lastCmdResult", ""))
        fresh_error = result != state.command_baseline and result.startswith(("[JUDGER_ERROR]", "[TIMEOUT]"))
        if current > state.sent_round and (state.marker in result or fresh_error):
            state = replace(append_trace(state, result), phase="need-llm")
        elif current - state.sent_round >= timeout_rounds:
            state = replace(append_trace(state, "Command result not correlated. Inspect state before repeating side effects."), phase="need-llm")
        else:
            return TaskWork(state)
    elif state.phase == "submitted":
        if current <= state.sent_round:
            return TaskWork(state)
        feedback = json.dumps(world.observation.raw.get("errors", []), ensure_ascii=False)
        state = replace(append_trace(state, "Task still active after submission. Previous answer: " + state.answer + "\nFeedback: " + feedback),
                        phase="need-llm", answer="")
    if state.phase == "answer-ready":
        nearby = [task for task in world.observation.raw["teamOur"].get("playerTasks", [])
                  if min(pioneer.pos.distance(pos) for pos in world.task_cells(task)) == 1]
        expected_score, readiness = 0.0, 25.0
        if len(nearby) == 1 and type(nearby[0].get("scoreReward")) is int:
            base, standard = nearby[0]["scoreReward"], nearby[0].get("timeoutRounds")
            # Acceptance/feedback timing is not calibrated: use a conservative
            # estimate for ranking, never report this as an actual award.
            elapsed = max(1, current - state.started + 2)
            speed = 5 * standard / elapsed if type(standard) is int and standard > 0 else 0
            expected_score, readiness = 0.5 * (base + speed), 0.0
        candidate = Candidate(f"answer:{state.started}", "submit-task-answer", frozenset({pioneer.id}),
                              (Action(pioneer.id, "submitAnswer", answer=state.answer),), Value(score=expected_score, readiness=readiness), "submit",
                              ("active-task", "correlated-llm-answer", "correctness-unverified"))
        return TaskWork(state, candidate=candidate)
    if state.phase in {"budget-exhausted"}:
        return TaskWork(state)
    if state.step >= max_requests:
        return TaskWork(replace(state, phase="budget-exhausted"))
    token = request_token(world, state)
    instructions = (
        "Solve the active programming-contest task using only the task sandbox and provided task facts. "
        "The sandbox has basic shell/Python and no external network; its tools execute separately from the game agent. "
        "Prefer bounded read-only inspection. Do not delete unrelated data, request secrets, change the game service, "
        "or assume unobserved files/results. Each command must finish within 15 seconds; keep output focused. "
        "A marker only correlates a result; nonzero exitCode, TIMEOUT, JUDGER_ERROR and TRUNCATED are not proof of success. "
        "Return ONLY one JSON object with the exact requestId: "
        '{"requestId":"...","kind":"execute","command":"..."} or '
        '{"requestId":"...","kind":"answer","answer":"exact task answer string"}. '
        "Do not answer until sufficient evidence is available. An answer is submitted as taskAnswer without additional prose."
    )
    prompt = json.dumps({"instructions": instructions, "requestId": token, "task": state.text,
                         "history": state.trace, "remainingToolRequests": max_requests - state.step}, ensure_ascii=False)
    return TaskWork(replace(state, phase="waiting-llm", step=state.step + 1, request_id=token, sent_round=current), prompt=prompt)


def after_selection(work: TaskWork, actions: tuple[Action, ...], round_no: int) -> TaskState | None:
    if work.state is not None and any(action.kind == "submitAnswer" for action in actions):
        return replace(work.state, phase="submitted", sent_round=round_no)
    return work.state
