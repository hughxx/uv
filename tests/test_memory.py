import unittest

from tests.helpers import packet
from agent.application import AgentApplication
from agent.memory import TurnMemory
from agent.protocol import Observation, idle_response


class MemoryTests(unittest.TestCase):
    def test_repeated_request_does_not_repeat_decision_or_state_commit(self):
        class CountingApplication(AgentApplication):
            calls = 0
            def decide(self, observation):
                self.calls += 1
                return idle_response()

        app = CountingApplication()
        payload = packet()
        first = app.handle_turn(payload)
        first["roleCommandMap"]["modified"] = {}
        self.assertEqual(app.handle_turn(payload), idle_response())
        self.assertEqual((app.calls, app.memory.revision), (1, 1))

    def test_same_round_conflict_and_unseen_older_request_do_not_advance(self):
        app = AgentApplication()
        app.handle_turn(packet(round_no=5))
        app.handle_turn(packet(round_no=5, gold=0))
        app.handle_turn(packet(round_no=1))
        self.assertEqual(app.memory.revision, 1)

    def test_side_change_resets_but_late_old_side_does_not(self):
        app = AgentApplication()
        first = packet(round_no=100)
        app.handle_turn(first)
        second = packet(round_no=1)
        second["teamOur"]["type"] = "defender"
        app.handle_turn(second)
        self.assertEqual(app.memory.last.round_no, 1)
        app.handle_turn(first)
        self.assertEqual(app.memory.last.raw["teamOur"]["type"], "defender")
        self.assertEqual(app.memory.revision, 2)

    def test_news_dedup_snapshot_isolation_and_time_event(self):
        memory = TurnMemory()
        first = packet(worldNews={"folkLegends": "test clue"})
        memory.commit(Observation.from_payload(first), idle_response())
        first["worldNews"]["folkLegends"] = "mutated"
        self.assertEqual(memory.last.raw["worldNews"]["folkLegends"], "test clue")
        memory.commit(Observation.from_payload(packet(round_no=2, worldNews={"folkLegends": "test clue"})), idle_response())
        self.assertEqual(memory.news, [("folkLegends", "test clue")])
        self.assertEqual([event.kind for event in memory.events], ["tick"])

    def test_bad_response_does_not_commit(self):
        memory = TurnMemory()
        with self.assertRaises(ValueError):
            memory.commit(Observation.from_payload(packet()), {})
        self.assertIsNone(memory.last)
