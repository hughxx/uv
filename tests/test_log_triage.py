import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from scripts.log_triage import MAX_OBJECT_CHARS, OVERVIEW_BYTES, SCHEMA, decode_events, extract, objects, read_cache, summarize


def event(kind, **fields):
    return {"schema": SCHEMA, "event": kind, **fields}


def startup(build="4f08f51bf075fd8b"):
    return event("listening", build=build, python="3.11.10", rules={"round_origin": 1, "day_length": 70, "night_length": 60})


def turn(round_no, *, alive=3, status="new", side="defender", **changes):
    row = event("turn", id=round_no, round=round_no, http=200, status=status, responseWritten=True, elapsedMs=57,
                input={"our": {"side": side, "gold": 75, "units": [{"id": 20010, "kind": "worker", "pos": [30, 7], "health": 220},
                                                                           {"id": 20013, "kind": "station", "pos": [30, 10], "health": 1500}] if alive else []},
                       "phaseTask": {"type": "str", "chars": 0}},
                decision={"livingActors": alive, "daytime": round_no < 71, "weapons": 3, "robots": 0, "selected": [],
                          "taskPhase": "inactive", "failedPreviousActions": [], "errorCodes": []},
                actionCount=0, actions={}, promptChars=0, executeCmdChars=0)
    row.update(changes)
    return row


def received(identifier):
    return event("request_received", id=identifier, method="POST", declaredBytes=3740)


class LogTriageTests(unittest.TestCase):
    def test_new_diagnostics_survive_wire_and_allowlisted_focus(self):
        from agent.telemetry import event_text
        from scripts.log_triage import focused

        decision = {"returnCheck": {"status": "ready", "required": 2, "available_moves": 0, "before": [0, 14], "after": [0, 13]},
                    "defense": [{"weapon": "10030", "selected": [[["10012"], "hold-weapon"]]}],
                    "task": {"result": {"status": "exit", "exitCode": 1, "markerSeen": True,
                                        "errorHints": ["FileNotFoundError", "PRIVATE_UNKNOWN"], "message": "PRIVATE_TEXT"}}}
        wire = event_text("turn", id=71, round=71, decision=decision)
        row = next(decode_events(wire, Counter()))
        defense = focused(row, "defense")["decision"]
        self.assertEqual(defense["defense"][0]["selected"], [[["10012"], "hold-weapon"]])
        self.assertEqual(defense["returnCheck"], decision["returnCheck"])
        task = focused(row, "task")["decision"]["task"]["result"]
        self.assertEqual(task["errorHints"], ["FileNotFoundError", "unknown"])
        self.assertTrue(task["markerSeen"])
        self.assertNotIn("PRIVATE", json.dumps(row))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="coregeek triage ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "PRIVATE_SOURCE_NAME.log"
        self.output = self.root / "summary"

    def analyze(self, rows, *, reverse=False, glue=False, encoding="utf-8"):
        lines = ["2026-09-15 02:10:16,392 | INFO | INFO:agent.server:" + json.dumps(row, ensure_ascii=False) for row in rows]
        if reverse:
            lines.reverse()
        self.source.write_text(("" if glue else "\n").join(lines), encoding=encoding)
        return summarize(self.source, self.output)

    def cache_events(self):
        connection, _ = read_cache(self.output)
        try:
            return [(segment, json.loads(payload)) for segment, payload in connection.execute("SELECT segment,payload FROM events ORDER BY seq")]
        finally:
            connection.close()

    def test_real_prefix_and_platform_noise_are_parsed_without_echoing_noise(self):
        self.source.write_text("process start...\nloop times: 2\n/private/python/path\n" +
                               "\n".join("2026-09-15 02:10:16,392 | INFO | INFO:agent.server:" + json.dumps(row)
                                         for row in (startup(), received(1), turn(1))), encoding="utf-8")
        metadata, segments = summarize(self.source, self.output)
        self.assertEqual(metadata["scan"]["recognizedEvents"], 3)
        self.assertEqual(segments[0]["rounds"], [1, 1])
        self.assertEqual(segments[0]["config"]["build"], "4f08f51bf075fd8b")
        self.assertNotIn("loop times", (self.output / "overview.md").read_text(encoding="utf-8"))

    def test_reported_round_one_builds_and_moves_are_preserved(self):
        raw = turn(1, elapsedMs=817.892, actionCount=3, actions={
            "20010": {"action": "build", "targetPos": [{"x": 29, "y": 8}], "name": "railgun"},
            "20011": {"action": "move", "targetPos": [{"x": 31, "y": 7}]},
            "20012": {"action": "build", "targetPos": [{"x": 32, "y": 9}], "name": "railgun"}})
        raw["decision"].update(weapons=0, candidates=37, selected=[["build-defense", "build", ["20010"]],
                                                                   ["acquire-task", "approach-task", ["20011"]], ["build-defense", "build", ["20012"]]],
                               search={"visited": 396, "exhausted": False, "rejections": {"action_validation@actions.py:123": 32}})
        _, summaries = self.analyze([startup(), received(1), raw, received(2)])
        self.assertEqual(summaries[0]["actions"], {"build": 2, "move": 1})
        self.assertEqual(summaries[0]["elapsedMs"]["max"], 817.892)
        self.assertEqual(summaries[0]["totals"]["requestIdsWithoutTurn"], 1)
        parsed = next(row for _, row in self.cache_events() if row["event"] == "turn")
        self.assertEqual(parsed["actions"], raw["actions"])
        self.assertEqual(parsed["decision"]["search"]["rejections"], {"action_validation@actions.py:123": 32})

    def test_glued_events_and_multiline_json(self):
        text = json.dumps(startup(), indent=2) + json.dumps(received(1)) + json.dumps(turn(1), indent=2)
        stats = Counter()
        rows = [event for _, text in objects(io.StringIO(text), stats) for event in decode_events(text, stats)]
        self.assertEqual([row["event"] for row in rows], ["listening", "request_received", "turn"])

    def test_json_export_wrapper_and_array_preserve_all_embedded_events(self):
        wrapped = [{"message": "prefix:" + json.dumps(startup())}, {"message": json.dumps(received(1)) + "\n" + json.dumps(turn(1))}]
        stats = Counter()
        rows = [row for _, text in objects(io.StringIO(json.dumps(wrapped)), stats) for row in decode_events(text, stats)]
        self.assertEqual(len(rows), 3)

    def test_braces_quotes_and_escaped_newlines_in_private_text_do_not_split_events(self):
        raw = turn(1, prompt='PRIVATE { "text": "}\n" } and \\ characters')
        stats = Counter()
        rows = [row for _, text in objects(io.StringIO(json.dumps(raw)), stats) for row in decode_events(text, stats)]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("PRIVATE", json.dumps(rows))

    def test_recovers_after_truncated_object_and_records_final_truncation(self):
        self.source.write_text('{"schema":"coregeek-online-v1","event":"turn","x":"broken\n' +
                               json.dumps(startup(), separators=(",", ":")) + json.dumps(turn(2), separators=(",", ":")) +
                               '{"schema":"coregeek-online-v1","event":', encoding="utf-8")
        metadata, segments = summarize(self.source, self.output)
        self.assertEqual(metadata["scan"]["recoveredTruncations"], 1)
        self.assertEqual(metadata["scan"]["incompleteObjects"], 1)
        self.assertEqual(segments[0]["rounds"], [2, 2])

    def test_oversized_object_does_not_hide_following_event(self):
        stats = Counter()
        text = '{"private":"' + "x" * (MAX_OBJECT_CHARS + 10) + '"}' + json.dumps(startup())
        rows = [row for _, part in objects(io.StringIO(text), stats) for row in decode_events(part, stats)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["oversizedObjects"], 1)

    def test_auto_reverses_newest_first_export(self):
        metadata, segments = self.analyze([startup(), received(1), turn(1), received(2), turn(2)], reverse=True)
        self.assertEqual(metadata["order"], "reverse")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["startReason"], "listening")
        self.assertEqual(segments[0]["totals"].get("requestIdsWithoutTurn", 0), 0)

    def test_utf16_bom_is_detected(self):
        metadata, segments = self.analyze([startup(), turn(1)], encoding="utf-16")
        self.assertEqual(metadata["encoding"], "utf-16")
        self.assertEqual(len(segments), 1)

    def test_restart_and_explicit_new_session_split_but_old_round_does_not(self):
        rows = [startup(), received(80), turn(80), received(81), turn(81), received(1), turn(1, status="cached"),
                received(2), turn(2, status="new_session", side="challenger"), startup("c1d6260e17d7aa8a"), received(1), turn(1)]
        _, segments = self.analyze(rows)
        self.assertEqual(len(segments), 3)
        self.assertEqual([item["startReason"] for item in segments], ["listening", "new_session", "listening"])
        self.assertEqual(segments[0]["rounds"], [80, 81])
        self.assertEqual(segments[1]["totals"].get("turnIdsWithoutRequest", 0), 0)

    def test_missing_pair_duplicate_new_and_cached_do_not_inflate_action_statistics(self):
        built = turn(1, actionCount=1, actions={"20010": {"action": "build", "name": "railgun"}})
        duplicate = copy.deepcopy(built)
        cached = copy.deepcopy(built)
        cached["status"] = "cached"
        _, summaries = self.analyze([startup(), received(1), built, duplicate, cached, received(9), turn(3)])
        result = summaries[0]
        self.assertEqual(result["actions"]["build"], 1)
        self.assertEqual(result["totals"]["duplicateNewRounds"], 1)
        self.assertEqual(result["totals"]["requestIdsWithoutTurn"], 1)
        self.assertEqual(result["totals"]["turnIdsWithoutRequest"], 1)
        self.assertEqual(result["missingNewRounds"], 1)

    def test_no_living_idle_tail_is_separate_and_overview_is_bounded(self):
        rows = [startup()]
        for round_no in range(1, 630):
            rows.extend((received(round_no), turn(round_no, alive=3 if round_no < 100 else 0)))
        _, summaries = self.analyze(rows, glue=True)
        result = summaries[0]
        self.assertEqual(result["totals"]["distinctNewRounds"], 629)
        self.assertEqual(result["totals"]["noLivingActors"], 530)
        self.assertEqual(result["longestNoLivingIdle"], [100, 629])
        self.assertLessEqual((self.output / "overview.md").stat().st_size, OVERVIEW_BYTES)

    def test_missing_feedback_is_reported_as_uncovered_not_proven_success(self):
        row = turn(1)
        del row["decision"]["failedPreviousActions"]
        del row["decision"]["errorCodes"]
        _, summaries = self.analyze([startup(), row])
        self.assertEqual(summaries[0]["fieldCoverage"]["failedPreviousActions"], 0)
        self.assertEqual(summaries[0]["fieldCoverage"]["robotSnapshot"], 0)

    def test_tool_requests_and_hold_are_not_misclassified_as_idle(self):
        held, tool = turn(1), turn(2, promptChars=300)
        held["decision"]["selected"] = [["operate-defense", "hold-weapon", ["20010"]]]
        _, summaries = self.analyze([startup(), held, tool])
        self.assertEqual(summaries[0]["totals"]["idleWhileAliveExcludingHold"], 0)
        self.assertEqual(summaries[0]["totals"]["positionedHoldRounds"], 1)

    def test_oscillation_bookmark_is_a_signal_not_a_claim_about_enemies(self):
        rows = [startup()]
        for round_no, pos in enumerate(([30, 7], [31, 7], [30, 7], [31, 7]), 71):
            row = turn(round_no)
            row["input"]["our"]["units"][0]["pos"] = pos
            row["decision"]["selected"] = [["operate-defense", "approach-weapon", ["20010"]]]
            rows.append(row)
        _, summaries = self.analyze(rows)
        self.assertTrue(any("待核验" in item["kind"] for item in summaries[0]["bookmarks"]))

    def test_sensitive_values_never_enter_cache_or_exports(self):
        raw = turn(1, prompt="PRIVATE_PROMPT", executeCmd="PRIVATE_COMMAND", Authorization="PRIVATE_AUTH")
        raw["actions"] = {"20011": {"action": "submitAnswer", "taskAnswer": "PRIVATE_ANSWER"}}
        raw["input"]["our"].update(teamId="PRIVATE_ID", teamName="PRIVATE_NAME")
        raw["decision"]["task"] = {"command": "PRIVATE_COMMAND", "lastEvent": "PRIVATE_EVENT"}
        raw["error"] = {"type": "ValueError", "message": "PRIVATE_DETAIL", "frames": ["C:/PRIVATE_PATH/a.py:1:main"]}
        self.analyze([startup(), raw])
        details = self.root / "details"
        extract(self.output, details, segment=1)
        for path in list(self.output.iterdir()) + list(details.iterdir()):
            if path.is_file():
                self.assertNotIn(b"PRIVATE_", path.read_bytes(), path.name)

    def test_extract_rounds_focus_pagination_and_event_limit(self):
        self.analyze([startup(), *(turn(round_no) for round_no in range(1, 41))])
        details = self.root / "details"
        manifest = extract(self.output, details, segment=1, rounds=(5, 30), focus="task", part_bytes=4096, limit=12)
        self.assertEqual((manifest["matchingEvents"], manifest["writtenEvents"], manifest["notExportedByLimit"]), (26, 12, 14))
        self.assertGreater(len(manifest["parts"]), 1)
        for part in manifest["parts"]:
            path = details / part["file"]
            self.assertLessEqual(path.stat().st_size, 4096)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["segment"], 1)
            self.assertTrue(all(5 <= row["round"] <= 30 for row in rows[1:]))
            self.assertTrue(all("robotSnapshot" not in row.get("decision", {}) for row in rows[1:]))

    def test_transport_focus_correlates_arrivals_with_selected_turns(self):
        self.analyze([startup(), received(1), turn(1), received(2), turn(2)])
        manifest = extract(self.output, self.root / "details", segment=1, rounds=(2, 2), focus="transport")
        self.assertEqual(manifest["writtenEvents"], 2)

    def test_existing_outputs_are_preserved_and_bad_segment_creates_nothing(self):
        self.analyze([startup(), turn(1)])
        before = (self.output / "overview.md").read_bytes()
        with self.assertRaises(FileExistsError):
            summarize(self.source, self.output)
        self.assertEqual(before, (self.output / "overview.md").read_bytes())
        details = self.root / "missing"
        with self.assertRaises(ValueError):
            extract(self.output, details, segment=999)
        self.assertFalse(details.exists())

    def test_unrecognized_legacy_log_is_not_reported_as_no_actions(self):
        self.source.write_text("round 1 -> {'action':'move'}\n", encoding="utf-8")
        _, segments = summarize(self.source, self.output)
        self.assertEqual(segments, [])
        self.assertIn("不能认定程序没有行动", (self.output / "overview.md").read_text(encoding="utf-8"))

    def test_malformed_event_type_and_startup_failures_remain_diagnosable(self):
        self.source.write_text(json.dumps({"schema": SCHEMA, "event": []}) +
                               json.dumps(event("fatal", error={"type": "OSError", "message": "PRIVATE_DETAIL"})), encoding="utf-8")
        metadata, segments = summarize(self.source, self.output)
        self.assertEqual(metadata["scan"]["unrecognizedObjects"], 1)
        self.assertEqual(segments[0]["totals"]["fatal"], 1)
        self.assertIn("fatal/telemetry_error/http_rejected=1/0/0", (self.output / "overview.md").read_text(encoding="utf-8"))

    def test_many_segments_do_not_make_overview_exceed_limit(self):
        rows = [row for _ in range(40) for row in (startup(), received(1), turn(1))]
        _, segments = self.analyze(rows)
        self.assertEqual(len(segments), 40)
        self.assertLessEqual((self.output / "overview.md").stat().st_size, OVERVIEW_BYTES)
        self.assertIn("其余段未展开", (self.output / "overview.md").read_text(encoding="utf-8"))

    def test_cli_runs_from_unrelated_directory(self):
        self.source.write_text(json.dumps(startup()) + json.dumps(turn(1)), encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "log_triage.py"
        completed = subprocess.run([sys.executable, "-B", str(script), "summary", str(self.source), "--out", str(self.output)],
                                   cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        completed = subprocess.run([sys.executable, "-B", str(script), "extract", str(self.output), "--segment", "1", "--rounds", "1:1",
                                    "--out", str(self.root / "details")], cwd=self.root, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
